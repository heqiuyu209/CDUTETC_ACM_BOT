"""集中配置：所有需要改动的参数都放这里，通过环境变量覆盖。

安全约定：
- 本文件不含任何真实密钥；密钥一律通过环境变量注入。
- 日志与报错严禁打印 AppSecret / API Key 明文。
- 权限默认全拒绝：未配置允许群/老师/超管白名单时，机器人不服务任何群、不允许任何人管理。
"""

import os
import shutil
from datetime import timedelta
from math import isfinite
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录：本文件位于 <root>/cdutetc/config.py，上一级才是根。
# .env、.env.example 与默认数据库都必须落在根目录，不能落进包内。
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"
ENV_EXAMPLE = BASE_DIR / ".env.example"


def _bootstrap_env_file() -> bool:
    """首次运行时用 .env.example 生成 .env，避免用户漏配必填项。

    只在 .env 不存在时创建，绝不覆盖已有文件（保护真实密钥）。
    Returns: 是否新建了 .env。
    """
    if ENV_FILE.exists() or not ENV_EXAMPLE.exists():
        return False
    try:
        shutil.copyfile(ENV_EXAMPLE, ENV_FILE)
        return True
    except OSError:
        return False


def _env_keys(text: str) -> list[str]:
    """解析 .env 文本里已定义的键（忽略注释与空行）。"""
    keys: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        keys.append(stripped.split("=", 1)[0].strip())
    return [k for k in keys if k]


def _sync_env_keys() -> list[str]:
    """把 .env.example 里有、而 .env 里缺的键追加到 .env 末尾。

    只追加缺失键，绝不修改或删除已有行（保护已填写的真实值）。
    这样拉取新版本后新增的配置项会自动出现在 .env 里，用户只需补值。
    Returns: 本次追加的键名列表。
    """
    if not ENV_FILE.exists() or not ENV_EXAMPLE.exists():
        return []
    try:
        current_text = ENV_FILE.read_text(encoding="utf-8")
        template_text = ENV_EXAMPLE.read_text(encoding="utf-8")
    except OSError:
        return []
    existing = set(_env_keys(current_text))

    # 按「注释块 + 赋值行」分组，补齐时保留原有说明文字
    blocks: list[tuple[list[str], str, str]] = []
    comments: list[str] = []
    for line in template_text.splitlines():
        stripped = line.strip()
        if not stripped:
            comments = []
            continue
        if stripped.startswith("#"):
            comments.append(line)
            continue
        if "=" not in stripped:
            comments = []
            continue
        key = stripped.split("=", 1)[0].strip()
        blocks.append((comments, line, key))
        comments = []

    additions: list[str] = []
    added_keys: list[str] = []
    for comments, assignment, key in blocks:
        if not key or key in existing:
            continue
        if key == "ACM_REMIND_OFFSET_HOURS" and "ACM_REMIND_BEFORE_HOURS" in existing:
            # 旧单节点部署继续回退到原值；自动补入24,1会静默改变提醒行为。
            continue
        additions.extend(comments)
        additions.append(assignment)
        added_keys.append(key)

    if not additions:
        return []
    try:
        with ENV_FILE.open("a", encoding="utf-8") as handle:
            if not current_text.endswith("\n"):
                handle.write("\n")
            handle.write("\n# ===== 以下为本次自动补齐的新增配置项 =====\n")
            handle.write("\n".join(additions))
            handle.write("\n")
    except OSError:
        return []
    return added_keys


ENV_CREATED = _bootstrap_env_file()
ENV_ADDED_KEYS = _sync_env_keys()

# 加载项目根目录下的 .env（若存在）。系统环境变量优先，.env 不覆盖已注入的值。
load_dotenv(ENV_FILE, override=False)

# .env 里写成 `KEY=` 的空值会被 os.getenv 当成「已设置」，从而吃掉代码里的
# 默认值（曾导致 ACM_DB_PATH 为空 → 连到临时库 → 机器人整体不可用）。
# 这里统一把空的 ACM_* 变量移除，让默认值生效；对本来默认就是空串的项无影响。
def _drop_empty_env(environ=None) -> list[str]:
    """删除值为空的 ACM_* 环境变量，返回被删除的键名。"""
    target = os.environ if environ is None else environ
    removed = [
        key for key in list(target)
        if key.startswith("ACM_") and not str(target[key]).strip()
    ]
    for key in removed:
        target.pop(key, None)
    return removed


ENV_DROPPED_EMPTY = _drop_empty_env()

# ================= QQ 机器人接入（OneBot/NapCat，必填） =================
# NapCatQQ 正向 WebSocket 地址，例如 ws://127.0.0.1:3001
# ================= 接入协议（二选一） =================
# onebot = 个人 QQ（NapCat 正向 WebSocket）；qqbot = QQ 开放平台官方机器人。
# 业务逻辑完全一致，只是接入层不同。
BOT_PROTOCOL = os.getenv("ACM_BOT_PROTOCOL", "onebot").strip().lower()
if BOT_PROTOCOL not in ("onebot", "qqbot"):
    raise RuntimeError(
        f"ACM_BOT_PROTOCOL 只能是 onebot 或 qqbot，当前为 {BOT_PROTOCOL!r}"
    )

# ---- 官方机器人（qqbot）凭证 ----
# 在 QQ 开放平台创建机器人后获得；WebSocket 模式不需要公网 IP 或域名。
QQBOT_APPID = os.getenv("ACM_QQBOT_APPID", "")
QQBOT_SECRET = os.getenv("ACM_QQBOT_SECRET", "")

# ================= QQ 机器人接入（OneBot/NapCat） =================
# NapCatQQ 正向 WebSocket 地址，例如 ws://127.0.0.1:3001
ONEBOT_WS_URL = os.getenv("ACM_ONEBOT_WS_URL", "")
# 可选鉴权 token（与 NapCat 配置一致；未开启留空）
ONEBOT_TOKEN = os.getenv("ACM_ONEBOT_TOKEN", "")
# 机器人自身 QQ 号（用于识别群内 @ 目标；缺省使用 OneBot 事件的 self_id）
BOT_QQ = os.getenv("ACM_BOT_QQ", "")
# WebSocket 静默超时（秒）：超过该时长收不到任何事件（含 NapCat 心跳）就主动
# 断开重连，用于兜住 TCP 半开连接导致的「机器人突然不回消息且不恢复」。
# NapCat 默认心跳 5 秒左右，180 秒足够宽松；设 0 可关闭该看门狗。
WS_IDLE_TIMEOUT_SECONDS = float(os.getenv("ACM_WS_IDLE_TIMEOUT_SECONDS", "180"))
# 普通 QQ 无平台主动消息限制；保留开关以便在需要时暂停提醒投递。
GROUP_ACTIVE_MESSAGES_ENABLED = os.getenv("ACM_GROUP_ACTIVE_MESSAGES_ENABLED", "1") == "1"

# ================= 大模型（OpenAI 兼容接口，必填） =================
LLM_API_KEY = os.getenv("ACM_LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("ACM_LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_MODEL = os.getenv("ACM_LLM_MODEL", "deepseek-chat")

# ================= 群与角色权限（默认全拒绝） =================
# 允许机器人服务的 QQ 群号列表（逗号分隔）。未配置 = 不服务任何群。
# ENV_* 是配置文件基线，运行时可被 /加群 /加老师 扩充（见 roles.py）：
# 最终生效的 ALLOWED_GROUPS / TEACHER_WHITELIST = 基线 ∪ 运行时白名单。
# 注意：超管白名单只能通过配置文件的 ACM_SUPER_ADMIN 指定，不支持运行时添加，
# 避免“提权命令自己给自己发权限”的失控风险。
ENV_ALLOWED_GROUPS = {
    g.strip() for g in os.getenv("ACM_ALLOWED_GROUPS", "").split(",") if g.strip()
}
ALLOWED_GROUPS = set(ENV_ALLOWED_GROUPS)
# 超级管理员 QQ 号白名单（逗号分隔，可多人）：/修正 纠正比赛、维护全局信息、
# 管理征集与查看全部明细；未配置 = 超管命令全部拒绝。
SUPER_ADMIN = {
    m.strip() for m in os.getenv("ACM_SUPER_ADMIN", "").split(",") if m.strip()
}
# 老师 QQ 号白名单（逗号分隔，发布权并入此白名单，原发布者白名单已移除）：
# 可提交比赛发布（默认超管确认）、维护课程/FAQ、发起征集、查看汇总。
# 未配置 = 老师管理命令全部拒绝。
ENV_TEACHER_WHITELIST = {
    m.strip() for m in os.getenv("ACM_TEACHER_WHITELIST", "").split(",") if m.strip()
}
TEACHER_WHITELIST = set(ENV_TEACHER_WHITELIST)
# 未授权群在被 @ 或收到命令时，是否回一句带群号的提示（便于管理员取号后 /加群）。
NOTICE_UNAUTHORIZED_GROUP = os.getenv("ACM_NOTICE_UNAUTHORIZED_GROUP", "1") != "0"
# 同一未授权群的提示最小间隔（秒），防止被拉进群后刷屏
NOTICE_UNAUTHORIZED_INTERVAL = int(os.getenv("ACM_NOTICE_UNAUTHORIZED_INTERVAL", "60"))
# 群内是否必须 @机器人 才响应（默认必须）。
# 开启后，群里单独发 /帮助、发布 等都不会触发，避免与其他机器人抢命令；
# 设为 0 则恢复“无 @ 也能执行显式命令”的旧行为。私聊管理不受此限制。
GROUP_REQUIRE_AT = os.getenv("ACM_GROUP_REQUIRE_AT", "1") != "0"
# 发布命令前缀：授权成员发送“发布 <链接/信息>”才会入库，避免含链接的普通提问误触发
PUBLISH_CMD = os.getenv("ACM_PUBLISH_CMD", "发布")
# 私聊管理命令作用的默认群（超管私聊发 /FAQ添加、/课程添加 等命令时使用）；
# 留空自动取 ALLOWED_GROUPS 中排序首个群号。
ADMIN_GROUP = os.getenv("ACM_ADMIN_GROUP", "").strip()

# ================= 回答限制（五层防护） =================
# 在保留 llm.py 现有 system prompt 软约束的基础上，回答前增加五层硬限制：
#   1) 话题范围硬限制：与 ACM 明显无关的问题不调模型，直接回复拒绝话术；
#   2) 敏感内容拦截：用户输入 / 模型输出命中敏感词（政治、色情、暴力、广告、
#      诈骗、涉密等）即拒绝；
#   3) 用户权限：问答默认对全体群成员开放（学生本来就要提问）。
#      只有显式配置 ACM_QA_WHITELIST 才限制为白名单成员，留空即全群可问；
#      命中 ACM_QA_BLACKLIST 直接忽略不回复。
#      注意：不要回退成「老师 ∪ 超管」白名单，那会让普通成员被静默忽略。
#   4) 频率防刷：单用户对问答接口滑动窗口限流，超限返回提示话术；
#   5) 输出过滤：回复文本二次敏感检查 + 长度上限截断 + 禁止超长链接。
# 以上每层均可独立开关；全部默认开启。

# 调试开关：是否把群号/QQ号/消息前 50 字写入日志（默认关闭，避免留存个人信息）。
# 仅在初次部署采集白名单 openid 时临时设为 1。
DEBUG_LOG_IDENTITY = os.getenv("ACM_DEBUG_LOG_IDENTITY", "0") == "1"

# --- 1) 话题范围硬限制 ---
QA_TOPIC_GATE_ENABLED = os.getenv("ACM_QA_TOPIC_GATE_ENABLED", "1") != "0"
QA_OFFTOPIC_REPLY = os.getenv(
    "ACM_QA_OFFTOPIC_REPLY", "我只回答与 ACM 集训队、比赛、训练相关的问题哦~"
)

# --- 2) 敏感内容拦截 ---
QA_SENSITIVE_GATE_ENABLED = os.getenv("ACM_QA_SENSITIVE_GATE_ENABLED", "1") != "0"
QA_SENSITIVE_REPLY = os.getenv(
    "ACM_QA_SENSITIVE_REPLY", "这个问题我不方便回答，请换个话题~"
)
# 追加自定义敏感词（逗号分隔），与 content_filter.py 内置词表合并使用
QA_SENSITIVE_EXTRA_WORDS = {
    w.strip() for w in os.getenv("ACM_QA_SENSITIVE_EXTRA_WORDS", "").split(",") if w.strip()
}

# --- 3) 用户权限 ---
# 默认对全群开放（学生答疑的预期行为），仅以下两种方式限制：
#   - 在 ACM_QA_BLACKLIST 里拉黑个别成员（静默忽略）；
#   - 显式配置 ACM_QA_WHITELIST 后，仅白名单成员可提问。
# 不再回退到「老师 ∪ 超管」白名单：那样会让配了老师白名单的部署
# 变成“只有管理员能提问”，普通成员被静默忽略。
QA_WHITELIST_ENABLED = os.getenv("ACM_QA_WHITELIST_ENABLED", "1") != "0"
QA_BLACKLIST = {
    m.strip() for m in os.getenv("ACM_QA_BLACKLIST", "").split(",") if m.strip()
}
QA_WHITELIST = {
    m.strip() for m in os.getenv("ACM_QA_WHITELIST", "").split(",") if m.strip()
}
QA_WHITELIST_REPLY = os.getenv(
    "ACM_QA_WHITELIST_REPLY", "这个群暂时只允许指定成员提问哦~"
)

# --- 4) 频率防刷 ---
QA_RATE_LIMIT_ENABLED = os.getenv("ACM_QA_RATE_LIMIT_ENABLED", "1") != "0"
QA_RATE_LIMIT_SECONDS = int(os.getenv("ACM_QA_RATE_LIMIT_SECONDS", "10"))   # 窗口时长（秒）
QA_RATE_LIMIT_MAX_CALLS = int(os.getenv("ACM_QA_RATE_LIMIT_MAX_CALLS", "3"))  # 窗口内最大调用次数
QA_RATE_LIMIT_REPLY = os.getenv(
    "ACM_QA_RATE_LIMIT_REPLY", "你问得太快啦，稍等几秒再问吧~"
)

# --- 5) 输出过滤 ---
QA_OUTPUT_MAX_LENGTH = int(os.getenv("ACM_QA_OUTPUT_MAX_LENGTH", "800"))       # 回复长度上限（字符）
QA_OUTPUT_MAX_LINK_LENGTH = int(os.getenv("ACM_QA_OUTPUT_MAX_LINK_LENGTH", "60"))  # 链接最长长度（字符）
QA_OUTPUT_FILTERED_REPLY = os.getenv(
    "ACM_QA_OUTPUT_FILTERED_REPLY", "这条回复内容不合规，已被过滤，换个问法试试~"
)

# --- 6) 算法话题限制（题解/思路/概念科普一律不答，省 token） ---
# 命中算法相关提问（求解、要代码、调错、科普讲解等）直接拒绝并返回引导话术；
# 队务类问题（课程/比赛/通知等）不受影响，FAQ 命中优先于本限制。
QA_PROBLEM_GATE_ENABLED = os.getenv("ACM_QA_PROBLEM_GATE_ENABLED", "1") != "0"
QA_PROBLEM_REPLY = os.getenv(
    "ACM_QA_PROBLEM_REPLY",
    "我主要帮忙处理队务和比赛安排，算法题可以带到集训课上和老师、队友一起讨论。",
)
QA_PROBLEM_EXTRA_WORDS = {
    w.strip() for w in os.getenv("ACM_QA_PROBLEM_EXTRA_WORDS", "").split(",") if w.strip()
}

# 简短寒暄默认本地自然回复；可显式开启轻量模型，不携带群资料。
QA_SOCIAL_CHAT_ENABLED = os.getenv("ACM_QA_SOCIAL_CHAT_ENABLED", "1") != "0"
QA_SOCIAL_CHAT_USE_LLM = os.getenv("ACM_QA_SOCIAL_CHAT_USE_LLM", "0") == "1"

# --- 7) 普通成员队务问答范围：只答已录入内容 ---
# 开启后普通成员（非老师/超管）的提问只匹配 FAQ / 课程 / 校安排 / 已录入比赛：
# 命中则按库内内容作答，未命中收集为未答问题；默认不调用模型。
# 可选 FAQ 模型只从本群候选中选编号，不生成队务答案。
# 这样既不会出现“我没有数据库权限”这类模型臆造的管理端话术，
# 也把普通成员提问的 token 成本降为 0。老师/超管不受此限制。
QA_MEMBER_KB_ONLY = os.getenv("ACM_QA_MEMBER_KB_ONLY", "1") != "0"
QA_MEMBER_NO_MATCH_REPLY = os.getenv(
    "ACM_QA_MEMBER_NO_MATCH_REPLY",
    "这条我还没查到，已经记下了，等老师补充后就能查到。",
)
# 已录入比赛的本地检索：问题与比赛名称/时间的字符串相似度低于阈值即视为未命中。
QA_KB_MATCH_MIN_SCORE = float(os.getenv("ACM_QA_KB_MATCH_MIN_SCORE", "0.34"))
# 仅本地无法确定 FAQ 时让模型选择候选编号；默认关闭，保持零模型成本。
QA_FAQ_LLM_ENABLED = os.getenv("ACM_QA_FAQ_LLM_ENABLED", "0") == "1"

# ================= 链接抓取（SSRF 与资源限制） =================
# 域名白名单（逗号分隔，小写）。配置后仅白名单域名可抓取（重定向目标同样校验）
FETCH_ALLOWED_DOMAINS = {
    d.strip().lower()
    for d in os.getenv("ACM_FETCH_ALLOWED_DOMAINS", "").split(",")
    if d.strip()
}
FETCH_MAX_BYTES = int(os.getenv("ACM_FETCH_MAX_BYTES", "1000000"))        # 解压后响应体上限（字节）
FETCH_MAX_REDIRECTS = int(os.getenv("ACM_FETCH_MAX_REDIRECTS", "3"))      # 重定向跳数上限
FETCH_TIMEOUT = float(os.getenv("ACM_FETCH_TIMEOUT", "10"))               # 每跳 DNS/连接/读取/解析总超时（秒）
FETCH_CONCURRENCY = int(os.getenv("ACM_FETCH_CONCURRENCY", "2"))          # 同时抓取上限

# ================= 大模型资源限制 =================
LLM_MAX_INPUT_CHARS = int(os.getenv("ACM_LLM_MAX_INPUT_CHARS", "6000"))   # 单次请求输入长度上限
LLM_MAX_OUTPUT_TOKENS = int(os.getenv("ACM_LLM_MAX_OUTPUT_TOKENS", "2000"))  # 单次请求输出长度上限
LLM_CONCURRENCY = int(os.getenv("ACM_LLM_CONCURRENCY", "2"))              # 模型并发请求上限

# ================= 定时提醒 =================
TZ_NAME = "Asia/Shanghai"
REMIND_CHECK_INTERVAL_MINUTES = int(os.getenv("ACM_REMIND_CHECK_INTERVAL_MINUTES", "30"))
# 开赛计划独立于旧版提前小时数，批准时即持久化；扫描间隔不会被旧30分钟配置拖慢。
CONTEST_REMIND_OFFSETS_MINUTES = tuple(sorted({
    int(x.strip()) for x in os.getenv("ACM_CONTEST_REMIND_OFFSETS_MINUTES", "30,15").split(",")
    if x.strip()
}, reverse=True))
CONTEST_REMIND_CHECK_SECONDS = int(os.getenv("ACM_CONTEST_REMIND_CHECK_SECONDS", "30"))
CONTEST_REMIND_GRACE_SECONDS = int(os.getenv("ACM_CONTEST_REMIND_GRACE_SECONDS", "120"))
if (not CONTEST_REMIND_OFFSETS_MINUTES or any(x <= 0 for x in CONTEST_REMIND_OFFSETS_MINUTES)
        or not 5 <= CONTEST_REMIND_CHECK_SECONDS <= 60
        or not CONTEST_REMIND_CHECK_SECONDS <= CONTEST_REMIND_GRACE_SECONDS <= 600):
    raise RuntimeError("开赛提醒节点须为正分钟数，检查间隔须为5–60秒，宽限须介于检查间隔与600秒之间")
REMIND_BEFORE_HOURS = int(os.getenv("ACM_REMIND_BEFORE_HOURS", "24"))
if REMIND_CHECK_INTERVAL_MINUTES < 1 or REMIND_BEFORE_HOURS <= 0:
    raise RuntimeError("提醒扫描间隔和提前小时数必须大于零")
REMIND_TYPES = ("reg_deadline", "contest_time")
# 多节点提前提醒（小时，逗号分隔，支持小数）。例：24,1,0.167 表示
# 提前 24 小时、1 小时、10 分钟各提醒一次。每场活动不建独立定时器，
# 仍由周期扫描按「已进入哪个节点窗口」判定，资源占用与比赛数量无关。
# 留空则回退为单节点 REMIND_BEFORE_HOURS 的旧行为。
def _remind_offset_hours() -> tuple[float, ...]:
    """显式多节点优先；仅配置旧提前小时数时保留单节点行为。"""
    raw = os.getenv("ACM_REMIND_OFFSET_HOURS", "").strip()
    if not raw:
        raw = os.getenv("ACM_REMIND_BEFORE_HOURS", "").strip() or "24,1"
    try:
        offsets = {float(value) for value in raw.split(",") if value.strip()} or {float(REMIND_BEFORE_HOURS)}
        if any(not isfinite(value) or value <= 0 for value in offsets):
            raise ValueError
        # 过大的有限数字同样不能交给运行中的提醒扫描。
        for value in offsets:
            timedelta(hours=value)
    except (ValueError, OverflowError) as error:
        raise RuntimeError("提醒节点必须是可表示的正数小时，不能包含 NaN 或 Infinity") from error
    return tuple(sorted(offsets, reverse=True))


REMIND_OFFSET_HOURS = _remind_offset_hours()

# ================= 发布确认（超管私聊） =================
# 老师发布比赛后，整理结果先私聊推送给超管，超管回「同意/拒绝」才入库并通知群。
PUBLISH_CONFIRM_ENABLED = os.getenv("ACM_PUBLISH_CONFIRM_ENABLED", "1") != "0"
# 新发布的比赛默认只通知「发布它的那个群」（1=开启，推荐）。
# 设为 0 则恢复旧行为：发布即通知所有允许群。
PUBLISH_SCOPE_TO_SOURCE = os.getenv("ACM_PUBLISH_SCOPE_TO_SOURCE", "1") != "0"
# 老师用自然语言描述比赛（没带「发布」命令）时，是否让模型先判断意图，
# 判为「在发布比赛」就自动转入发布流程。关闭则只认显式命令。
NL_INTENT_ENABLED = os.getenv("ACM_NL_INTENT_ENABLED", "1") != "0"

# ================= 自动周赛（v2） =================
# 在提前提醒窗口前按模板生成周赛，启动时补最近未来场次；默认不通知全员，
# 超管确认后才通知全员或 /修正 调整/删除。地点不纳入管理。
WEEKLY_CONTEST_ENABLED = os.getenv("ACM_WEEKLY_CONTEST_ENABLED", "1") != "0"
WEEKLY_CONTEST_WEEKDAY = int(os.getenv("ACM_WEEKLY_CONTEST_WEEKDAY", "6"))   # 0=周一 ... 6=周日
WEEKLY_CONTEST_START = os.getenv("ACM_WEEKLY_CONTEST_START", "19:00")
WEEKLY_CONTEST_END = os.getenv("ACM_WEEKLY_CONTEST_END", "21:00")
WEEKLY_CONTEST_TEMPLATE = os.getenv(
    "ACM_WEEKLY_CONTEST_TEMPLATE",
    "ACM 周赛（本周）",
)
# 周赛只对明确指定的群可见并提醒。留空时不对任何群开放。
# 官方接入填写群 OpenID，不能填 QQ 群号。
WEEKLY_CONTEST_GROUPS = {
    g.strip() for g in os.getenv("ACM_WEEKLY_CONTEST_GROUPS", "").split(",") if g.strip()
}

# ================= 每日聚合晨报（主动额度节流） =================
# 官方协议每群每天主动消息额度只有 1~4 条，因此把「当日课程 + 临近截止征集 +
# 临近赛事」合并为每天一条晨报，代替过去多次主动推送。
DAILY_DIGEST_ENABLED = os.getenv("ACM_DAILY_DIGEST_ENABLED", "1") != "0"


def _digest_clock() -> tuple[int, int]:
    """解析晨报时刻（HH:MM）；非法值回落到 08:30，不让机器人因配置笔误起不来。"""
    raw = os.getenv("ACM_DAILY_DIGEST_TIME", "08:30").strip()
    try:
        hour_text, minute_text = raw.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(raw)
        return hour, minute
    except (ValueError, AttributeError):
        return 8, 30


DAILY_DIGEST_HOUR, DAILY_DIGEST_MINUTE = _digest_clock()
# 群员只读查询的被动回复尾部是否搭便车附带最紧急待办（零主动额度消耗）。
DAILY_DIGEST_PIGGYBACK_ENABLED = os.getenv("ACM_DAILY_DIGEST_PIGGYBACK_ENABLED", "1") != "0"

# ================= 公共来源低频集中抓取（v2，默认关闭） =================
# 自动抓取默认关闭：开启后同一来源两次抓取的最小间隔（分钟）由下值控制。
AUTO_FETCH_ENABLED = os.getenv("ACM_AUTO_FETCH_ENABLED", "0") != "0"
FETCH_MIN_INTERVAL_MINUTES = int(os.getenv("ACM_FETCH_MIN_INTERVAL_MINUTES", "60"))
# 自动抓取生成的记录默认不通知全员，由超管确认后处理（/修正 或 /比赛详情 人工核对）。

# ================= 本地数据 =================
# 本地数据目录：沿用上面已解析的项目根目录（字符串形式）
DB_PATH = os.getenv("ACM_DB_PATH", os.path.join(str(BASE_DIR), "contests.db"))

# ================= 启动前配置检查 =================

# 按所选协议决定必填项：OneBot 要 WS 地址，官方机器人要 AppID/AppSecret
if BOT_PROTOCOL == "qqbot":
    REQUIRED_CONFIG = {
        "ACM_QQBOT_APPID": QQBOT_APPID,
        "ACM_QQBOT_SECRET": QQBOT_SECRET,
        "ACM_LLM_API_KEY": LLM_API_KEY,
    }
else:
    REQUIRED_CONFIG = {
        "ACM_ONEBOT_WS_URL": ONEBOT_WS_URL,
        "ACM_LLM_API_KEY": LLM_API_KEY,
    }

# 不影响启动、但不配就等于没有功能的配置项（启动时只告警）
RECOMMENDED_CONFIG = {
    "ACM_ALLOWED_GROUPS": ENV_ALLOWED_GROUPS,
    "ACM_TEACHER_WHITELIST": ENV_TEACHER_WHITELIST,
    "ACM_SUPER_ADMIN": SUPER_ADMIN,
}


def check_config() -> None:
    """启动前检查必要配置；缺失时抛错，只列缺失项名，不打印密钥值。"""
    missing = [name for name, value in REQUIRED_CONFIG.items() if not value]
    if missing:
        raise RuntimeError(
            "缺少必要配置（请通过环境变量设置）: " + ", ".join(missing)
        )


def describe_empty_config() -> list[str]:
    """返回「已识别但留空」的建议配置项名，供启动日志提示。"""
    return [name for name, value in RECOMMENDED_CONFIG.items() if not value]
