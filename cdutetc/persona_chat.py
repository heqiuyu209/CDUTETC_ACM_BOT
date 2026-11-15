"""低 Token 拟人化群聊：每日生活状态机 + 三层记忆 + 漏斗触发。

设计要点：
  - 三层记忆：进程内滑窗（最近 6 条）→ bot_memory_facts（个人标签，≤5 条）→
    bot_daily_state（一句当前生活状态）；前两层按群/按人隔离，第三层全局单行。
  - 省 Token 优先：Prompt 静态人设 + 一句状态 + 最近 6 条上下文 + 最近 2~3 条标签，
    总输入按 PROMPT_TOKEN_BUDGET 硬约束，超预算就按阶梯先砍上下文再砍记忆。
  - 0 Token 规则记忆：命中「我叫/我是/我擅长/…」等正则时用确定性规则截取短语写库，
    绝不为记一句话再调一次模型。
  - 表情包顺风车：模型可在回复尾部附带 [MEME:目录]，代码剥离后复用阶段二的表情包链路，
    与关键词触发共用素材与缓存，但跳过关键词冷却（本条回复已明确点名分类）。
  - 主动插话只在高潮时发生：2 分钟 ≥12 条 + 距上次插话 ≥60 分钟 + 5% 概率三重闸门，
    任一条不满足都保持安静，避免变成刷屏机器人。
"""

from __future__ import annotations

import random
import re
import time
from collections import deque
from collections.abc import Callable

from cdutetc import config, llm, memes
from cdutetc import database as db

# ---------------- 每日生活状态机 ----------------

# 一天三次切换生活状态（Asia/Shanghai）；固定时刻而非等间隔，更贴合人的作息。
DAILY_STATE_SLOTS = ((12, 30), (18, 30), (23, 0))

# 内置语料：集训队日常，全部一句话、没有客服腔，随机取一条写库。
DAILY_STATUS_POOL = (
    "刚点完外卖，正在B站看算法区UP主整活",
    "刚被一道区间DP卡红温，正刷抖音回血",
    "正在整理今晚的补题清单",
    "在机房吹空调摸鱼中",
    "刚AK完一套大模拟，正在群里发呆",
    "在食堂排队，脑子里还在想线段树怎么写",
    "刚睡醒，正在把昨晚没写出来的题再抄一遍",
    "在宿舍抱着笔记本调了一小时TLE，准备去洗把脸",
)

# ---------------- Prompt 预算 ----------------

# 单次输入的总 Token 预算：低于 700 是硬要求，实际拼装通常 200~400。
PROMPT_TOKEN_BUDGET = 700
# 短期上下文条数上限。
CONTEXT_WINDOW = 6
# 个人标签注入条数上限（2~3 条：太少不像认识，太多既费 token 又容易串味）。
MEMORY_FACTS_INJECT = 3
# 模型输出上限：拟人聊天只要 1~2 句话，给多了必然啰嗦。
REPLY_MAX_TOKENS = 120
# 群聊里单条回复的字符上限：超过就截，避免刷屏。
REPLY_MAX_CHARS = 80

# 降级阶梯：(上下文条数, 记忆条数)。预算不够就顺着往下走，最后只剩当前一句。
_TRIM_LADDER = ((CONTEXT_WINDOW, MEMORY_FACTS_INJECT), (4, 3), (3, 2), (2, 1), (1, 0), (0, 0))

# ---------------- 表情包标签 ----------------

# 模型在回复末尾附带的分类标签；目录名限定为阶段二定义的五类。
MEME_TAG_RE = re.compile(r"\[\s*MEME\s*[:：]\s*([A-Za-z_]+)\s*\]", re.IGNORECASE)
MEME_DIRECTORIES = frozenset(memes.CATEGORIES)

# ---------------- 0 Token 记忆提取 ----------------

# 命中这些口语模板时，用确定性规则截取短语当个人标签，全程不调模型。
MEMORY_PATTERNS = (
    re.compile(r"我叫[^\s，,。.!！?？~～]{1,12}"),
    re.compile(r"我是[^\s，,。.!！?？~～]{1,12}"),
    re.compile(r"我擅长[^\s，,。.!！?？~～]{1,12}"),
    re.compile(r"我卡在[^\s，,。.!！?？~～]{1,12}"),
    re.compile(r"我喜欢[^\s，,。.!！?？~～]{1,12}"),
    re.compile(r"我讨厌[^\s，,。.!！?？~～]{1,12}"),
    re.compile(r"我\s?AC\s?了[^\s，,。.!！?？~～]{0,12}"),
    re.compile(r"我退役了?[^\s，,。.!！?？~～]{0,12}"),
)

# 客服腔标记：出现任意一个就整条丢弃，宁可不说也不装客服。
_SERVICE_TONE_MARKERS = (
    "作为ai", "作为一个ai", "作为人工智能", "有什么可以帮", "有什么能帮",
    "很高兴为您", "很高兴为你", "请问还有什么", "希望对您有帮助", "希望对你有帮助",
    "我是ai助手", "ai助手",
)

# 模型爱加的角色前缀，剥离掉更自然。
_PREFIX_RE = re.compile(r"^(?:学姐|我|回复|答)\s*[:：]\s*")

# ---------------- 主动插话闸门 ----------------

# 高潮判定窗口：2 分钟内至少 12 条群消息。
INTERJECT_WINDOW_SECONDS = 120.0
INTERJECT_MIN_MESSAGES = 12
# 同一群两次主动插话的最小间隔（60 分钟）。
INTERJECT_COOLDOWN_SECONDS = 3600.0
# 满足条数条件后真正插话的概率（5%）。
INTERJECT_PROBABILITY = 0.05

# 冷却表超过该规模时做一次惰性清理，防止长期运行后无界增长。
_COOLDOWN_PRUNE_SIZE = 2000


def estimate_tokens(text: str) -> int:
    """粗估 Token 数（偏保守，用于守住 Prompt 预算）。

    非 ASCII 字符按 1 Token/字，ASCII 按 4 字符/Token：中文场景下这样估比
    真实分词器略高，宁可多留余量也不越界。
    """
    if not isinstance(text, str) or not text:
        return 0
    ascii_count = sum(1 for char in text if ord(char) < 128)
    return (len(text) - ascii_count) + (ascii_count + 3) // 4


def extract_memory_fact(text: str, *, max_chars: int = db.MEMORY_FACT_MAX_CHARS) -> str:
    """用确定性规则从一句话里截取个人标签；没有可记内容返回空串。

    只认内置模板（我叫/我是/我擅长/我卡在/我喜欢/我讨厌/我AC了/我退役），
    取到的短语超过 max_chars 直接截断——标签是给人速览的，不是原话存档。
    """
    if not isinstance(text, str):
        return ""
    for pattern in MEMORY_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        fact = " ".join(match.group(0).split())
        if not fact:
            continue
        return fact[:max_chars]
    return ""


def split_meme_tag(reply: str) -> tuple[str, str]:
    """剥离 [MEME:目录] 标签，返回（纯文本, 合法分类）。

    无论标签是否合法都从正文里删掉：内部标记绝不能泄漏到群里。
    """
    if not isinstance(reply, str):
        return "", ""
    match = MEME_TAG_RE.search(reply)
    category = ""
    if match and match.group(1).lower() in MEME_DIRECTORIES:
        category = match.group(1).lower()
    cleaned = MEME_TAG_RE.sub("", reply)
    return clean_reply(cleaned), category


def clean_reply(text: str) -> str:
    """整理模型回复：单行、去角色前缀、去客服腔、限长。

    返回空串表示这句话不该发出去（客服腔或纯空白）。
    """
    if not isinstance(text, str):
        return ""
    line = " ".join(text.split())
    line = _PREFIX_RE.sub("", line).strip()
    if not line:
        return ""
    lowered = line.lower()
    if any(marker in lowered for marker in _SERVICE_TONE_MARKERS):
        return ""
    return line[:REPLY_MAX_CHARS]


def _system_prompt(status_text: str) -> str:
    """静态人设 + 一句当前状态：这部分几乎不变，是最省的写法。"""
    return (
        "你是成理工程 ACM 集训队的吉祥物学姐，群里都喊你学姐。\n"
        "说话像划水的老队员：短、口语、带自嘲，每次只回 1~2 句。\n"
        "黑话：WA、TLE、AK、大模拟、DP、线段树、补题、红温。\n"
        "禁止客服腔：不说「作为AI助手」「有什么可以帮您」「很高兴为您服务」。\n"
        f"你现在的生活状态：{status_text}\n"
        "若适合配图，在回复最后附一个 [MEME:ac|wa_tle|question|slacking|interrupt]，最多一个。"
    )


def _user_prompt(*, nickname: str, text: str, facts: list[str],
                 context: list[str]) -> str:
    """动态部分：最近上下文 + 对说话人的印象 + 本次要回的话。"""
    parts: list[str] = []
    if context:
        parts.append("群聊最近几句：\n" + "\n".join(context))
    if facts:
        parts.append(f"你对「{nickname}」的印象：{'；'.join(facts)}")
    parts.append(f"现在跟你说话的群友：「{nickname}」：{text}")
    parts.append("用学姐的口吻回 1~2 句，30 字内，直接说话，不要旁白。")
    return "\n".join(parts)


def build_conversation(*, nickname: str, text: str, status_text: str,
                       facts: list[str] | None = None,
                       context: list[str] | None = None,
                       budget: int = PROMPT_TOKEN_BUDGET) -> tuple[str, str]:
    """拼装（system, user）两条消息，总输入估算 Token 不超过 budget。

    预算吃紧时按 _TRIM_LADDER 先砍上下文、再砍记忆，最后只剩当前一句话；
    这样无论上下文多长都不会把单价顶上去。
    """
    contexts = [str(item) for item in (context or []) if str(item or "").strip()]
    labels = [str(item) for item in (facts or []) if str(item or "").strip()]
    nickname = str(nickname or "群友").strip() or "群友"
    text = str(text or "").strip()
    system = _system_prompt(str(status_text or "").strip() or db.DEFAULT_DAILY_STATUS)
    system_tokens = estimate_tokens(system)

    for context_limit, fact_limit in _TRIM_LADDER:
        user = _user_prompt(
            nickname=nickname,
            text=text[:200],
            facts=labels[:fact_limit],
            context=contexts[-context_limit:] if context_limit else [],
        )
        if system_tokens + estimate_tokens(user) <= budget:
            return system, user
    # 兜底：状态或原话本身极长时再硬截一次，保证预算绝不越界。
    trimmed = text[:80]
    user = _user_prompt(nickname=nickname, text=trimmed, facts=[], context=[])
    budget_left = max(budget - system_tokens, 0)
    while user and estimate_tokens(user) > budget_left:
        user = user[: max(len(user) - 20, 0)]
    return system, user


async def chat(system: str, user: str, *, temperature: float = 0.8,
               max_tokens: int = REPLY_MAX_TOKENS, timeout: float = 30.0) -> str:
    """调一次模型；未配置 API Key 时直接返回空串（调用方按「不说话」处理）。"""
    if not config.LLM_API_KEY:
        return ""
    return await llm._chat_once(system, user, temperature=temperature,
                                max_tokens=max_tokens, timeout=timeout)


# ---------------- 每日生活状态机 ----------------


def pick_daily_status(*, rng: Callable[[], float] = random.random) -> str:
    """从语料库里随机取一条状态（不走模型，零 Token）。"""
    pool = DAILY_STATUS_POOL
    index = min(int(rng() * len(pool)), len(pool) - 1)
    return pool[index]


def rotate_daily_state(*, rng: Callable[[], float] = random.random,
                       persist: Callable[[str], bool] | None = None) -> str:
    """切换一次生活状态并写库，返回新状态；写库失败也不抛，只返回文案。

    persist 可注入（测试用），默认写 bot_daily_state。
    """
    text = pick_daily_status(rng=rng)
    writer = persist or (lambda value: db.set_daily_state(value))
    try:
        writer(text)
    except Exception:
        return text
    return text


async def daily_state_job() -> str:
    """APScheduler 的 job 入口：每次触发切一条新状态。"""
    return rotate_daily_state()


# ---------------- 进程内短期记忆 ----------------


class PersonaMemory:
    """按群隔离的最近消息滑窗；纯内存、不写库，进程重启即清空。"""

    def __init__(self, *, window_size: int = CONTEXT_WINDOW) -> None:
        self.window_size = window_size
        self._windows: dict[str, deque] = {}

    def observe(self, group_id: str, nickname: str, text: str) -> None:
        """记录一条群消息（调用方负责过滤空文本与 CQ 段）。"""
        group = str(group_id or "").strip()
        content = " ".join(str(text or "").split())
        if not group or not content:
            return
        who = str(nickname or "群友").strip() or "群友"
        window = self._windows.get(group)
        if window is None:
            window = deque(maxlen=self.window_size)
            self._windows[group] = window
        window.append(f"[{who}]: {content[:60]}")

    def recent(self, group_id: str) -> list[str]:
        """取该群最近几条上下文（最旧在前）；没有记录返回空列表。"""
        window = self._windows.get(str(group_id or "").strip())
        return list(window) if window else []

    def reset(self, group_id: str | None = None) -> None:
        if group_id is None:
            self._windows.clear()
            return
        self._windows.pop(str(group_id), None)


class InterjectionGate:
    """群体高潮插话闸门：条数、冷却、概率三重校验，时钟与随机源可注入。"""

    def __init__(
        self,
        *,
        window_seconds: float = INTERJECT_WINDOW_SECONDS,
        min_messages: int = INTERJECT_MIN_MESSAGES,
        cooldown_seconds: float = INTERJECT_COOLDOWN_SECONDS,
        probability: float = INTERJECT_PROBABILITY,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.window_seconds = window_seconds
        self.min_messages = min_messages
        self.cooldown_seconds = cooldown_seconds
        self.probability = probability
        self._clock = clock
        self._rng = rng
        self._windows: dict[str, deque] = {}
        self._last_at: dict[str, float] = {}

    def observe(self, group_id: str, *, now: float | None = None) -> bool:
        """记录一条群消息；返回 True 表示这一次该主动插话。

        冷却先于概率：冷却期内的消息既不掷骰子也不刷新冷却；未达条数阈值同样
        不掷骰子——否则每条消息都在赌，5% 会被放大成必然事件。
        """
        group = str(group_id or "").strip()
        if not group:
            return False
        moment = self._clock() if now is None else now
        window = self._windows.get(group)
        if window is None:
            window = deque()
            self._windows[group] = window
        window.append(moment)
        deadline = moment - self.window_seconds
        while window and window[0] < deadline:
            window.popleft()
        if len(window) < self.min_messages:
            return False
        last = self._last_at.get(group)
        if last is not None and moment - last < self.cooldown_seconds:
            return False
        if self._rng() >= self.probability:
            return False
        self._last_at[group] = moment
        # 命中后清空窗口：同一波高潮只插一次，后续重新积累。
        window.clear()
        self._prune(moment)
        return True

    def _prune(self, moment: float) -> None:
        if len(self._last_at) <= _COOLDOWN_PRUNE_SIZE:
            return
        deadline = moment - self.cooldown_seconds
        for stale in [key for key, at in self._last_at.items() if at < deadline]:
            self._last_at.pop(stale, None)

    def reset(self, group_id: str | None = None) -> None:
        if group_id is None:
            self._windows.clear()
            self._last_at.clear()
            return
        group = str(group_id)
        self._windows.pop(group, None)
        self._last_at.pop(group, None)


_default_memory: PersonaMemory | None = None
_default_gate: InterjectionGate | None = None


def get_memory() -> PersonaMemory:
    """进程级单例：滑窗必须跨消息保留。"""
    global _default_memory
    if _default_memory is None:
        _default_memory = PersonaMemory()
    return _default_memory


def get_gate() -> InterjectionGate:
    """进程级单例：插话冷却必须跨消息保留。"""
    global _default_gate
    if _default_gate is None:
        _default_gate = InterjectionGate()
    return _default_gate


def reset_engine() -> None:
    """丢弃单例（测试隔离用）。"""
    global _default_memory, _default_gate
    _default_memory = None
    _default_gate = None
