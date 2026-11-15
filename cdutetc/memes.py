"""QQ 官方富媒体表情包引擎：目录即分类、关键词触发与 file_info 复用。

链路说明（官方协议发图两段式）：
  1) 把公网图片 URL 提交 ``/v2/groups/{group_openid}/files``（file_type=1）换 ``file_info``；
  2) 用 ``msg_type=7`` 的被动回复把 ``file_info`` 发进群。

设计要点：
  - ``assets/memes/<分类>/`` 目录即分类，目录不存在或为空时整条链路静默跳过；
  - ``ACM_MEME_BASE_URL`` 未配置（空串）时不发图，绝不拼出半截地址；
  - ``file_info`` 按 (群, 相对路径) 缓存进 ``meme_file_cache`` 表，有效期内直接复用，
    避免每次发图都调上传接口；
  - 关键词触发受「25% 概率 + 每群 5 分钟冷却」双重限制，防止刷屏。
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from cdutetc import config

# 素材根目录：随仓库分发的静态资源，部署时也可用静态服务器单独托管。
MEMES_DIR = Path(__file__).resolve().parent.parent / "assets" / "memes"

# 目录即分类：与 chat_fun.INTERRUPT_CATEGORY 对齐。
CATEGORY_AC = "ac"
CATEGORY_WA_TLE = "wa_tle"
CATEGORY_QUESTION = "question"
CATEGORY_SLACKING = "slacking"
CATEGORY_INTERRUPT = "interrupt"
CATEGORIES = (
    CATEGORY_AC,
    CATEGORY_WA_TLE,
    CATEGORY_QUESTION,
    CATEGORY_SLACKING,
    CATEGORY_INTERRUPT,
)

# 只有这些后缀当作图片；README.md 等占位文件不会被当成素材发出去。
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp")

# 英文高频词必须按词边界匹配：否则 little 会命中 TLE、make 会命中 AK。
_EDGE_LEFT = r"(?<![a-z0-9])"
_EDGE_RIGHT = r"(?![a-z0-9])"

# 命中关键词 → 分类；按顺序取第一条命中的规则。
_KEYWORD_RULES = (
    (CATEGORY_WA_TLE, re.compile(_EDGE_LEFT + "tle" + _EDGE_RIGHT, re.IGNORECASE)),
    (CATEGORY_WA_TLE, re.compile(_EDGE_LEFT + "wa" + _EDGE_RIGHT, re.IGNORECASE)),
    (CATEGORY_WA_TLE, re.compile("爆零|红温")),
    (CATEGORY_AC, re.compile(_EDGE_LEFT + "ak" + _EDGE_RIGHT, re.IGNORECASE)),
    (CATEGORY_AC, re.compile("太强了|膜拜")),
    (CATEGORY_SLACKING, re.compile("摸鱼|下班|早安|晚安")),
    (CATEGORY_QUESTION, re.compile(r"？{2,}|\?{2,}|啥意思")),
)

# 关键词触发的随机概率与每群冷却。
TRIGGER_PROBABILITY = 0.25
GROUP_COOLDOWN_SECONDS = 300.0


def match_category(text) -> str:
    """按关键词表判定消息属于哪个情绪分类；无命中返回空串。"""
    if not isinstance(text, str) or not text.strip():
        return ""
    for category, pattern in _KEYWORD_RULES:
        if pattern.search(text):
            return category
    return ""


def list_memes(category: str, *, root: Path | None = None) -> list[str]:
    """列出某分类下的可用素材，返回相对素材根目录的 POSIX 风格路径。

    目录不存在、不是目录或没有图片时统一返回空列表（调用方据此静默跳过）。
    """
    if category not in CATEGORIES:
        return []
    base = MEMES_DIR if root is None else Path(root)
    directory = base / category
    if not directory.is_dir():
        return []
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    names = [
        f"{category}/{path.name}"
        for path in entries
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    ]
    return sorted(names)


def pick_meme(category: str, *, root: Path | None = None,
              choice: Callable[..., str] = random.choice) -> str:
    """随机取一张素材的相对路径；分类无素材时返回空串。"""
    names = list_memes(category, root=root)
    if not names:
        return ""
    return choice(names)


def public_url(relative_path: str, *, base_url: str | None = None) -> str:
    """拼出公网可访问的图片地址；BASE_URL 未配置时返回空串，绝不发半截链接。"""
    base = (config.MEME_BASE_URL if base_url is None else base_url) or ""
    base = base.strip().rstrip("/")
    relative = str(relative_path or "").strip().lstrip("/")
    if not base or not relative:
        return ""
    return f"{base}/{quote(relative, safe='/')}"


@dataclass(frozen=True)
class MemeTrigger:
    """一次关键词触发的结论：分类 + 相对路径 + 公网地址。"""

    category: str
    relative_path: str
    url: str


class MemeEngine:
    """关键词触发的概率与冷却控制；时钟、随机源与素材目录均可注入。"""

    def __init__(
        self,
        *,
        probability: float = TRIGGER_PROBABILITY,
        cooldown_seconds: float = GROUP_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
        root: Path | None = None,
    ) -> None:
        self.probability = probability
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._rng = rng
        self._root = root
        self._last_at: dict[str, float] = {}

    def should_trigger(self, group_id: str, text: str, *,
                       now: float | None = None) -> str:
        """命中关键词且通过冷却与概率时返回分类名，否则返回空串。

        冷却先于概率判断：冷却期内的消息既不掷骰子也不刷新冷却时间。
        """
        category = match_category(text)
        if not category:
            return ""
        group = str(group_id or "").strip()
        if not group:
            return ""
        moment = self._clock() if now is None else now
        last = self._last_at.get(group)
        if last is not None and moment - last < self.cooldown_seconds:
            return ""
        if self._rng() >= self.probability:
            return ""
        self._last_at[group] = moment
        return category

    def resolve(self, category: str) -> MemeTrigger | None:
        """把分类解析成可发送的（相对路径, 公网地址）；任一步缺失即返回 None。"""
        relative = pick_meme(category, root=self._root)
        if not relative:
            return None
        url = public_url(relative)
        if not url:
            return None
        return MemeTrigger(category=category, relative_path=relative, url=url)

    def reset(self, group_id: str | None = None) -> None:
        if group_id is None:
            self._last_at.clear()
            return
        self._last_at.pop(str(group_id), None)


_default_engine: MemeEngine | None = None


def get_engine() -> MemeEngine:
    """进程级单例：每群冷却状态必须跨消息保留。"""
    global _default_engine
    if _default_engine is None:
        _default_engine = MemeEngine()
    return _default_engine


def reset_engine() -> None:
    """丢弃单例（测试隔离用）。"""
    global _default_engine
    _default_engine = None
