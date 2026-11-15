"""零 Token 群聊复读机：跟风 +1 与打断复读。

设计要点：
  - 每个群维护最近 5 条群消息的滑动窗口（文本、发送者、时刻），全内存、不写库；
  - 窗口末尾出现「3 个不同群友发同一句话」→ 直接用被动回复复读原文本，不调模型；
  - 同一句话 10 分钟内只复读一次，避免机器人与群友无限对喊刷屏；
  - 连续 5 个不同群友复读时以 15% 概率改发表情包打断（分类见 memes.INTERRUPT）；
  - 判断结果只返回动作对象，发送交给 main 的消息处理器，便于离线测试。
"""

from __future__ import annotations

import random
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

# 滑动窗口长度：窗口内最多保留 5 条群消息，正好容纳「5 个不同群友复读」。
WINDOW_SIZE = 5
# 触发跟风复读所需的不同发送者数量。
REPEAT_THRESHOLD = 3
# 触发打断表情包所需的不同发送者数量。
INTERRUPT_THRESHOLD = 5
# 达到打断阈值后真正发表情包的概率。
INTERRUPT_PROBABILITY = 0.15
# 同一句话的复读/打断冷却秒数（10 分钟）。
ECHO_COOLDOWN_SECONDS = 600.0
# 超长文本不参与复读：转发长文、日志刷屏不该被机器人复述一遍。
MAX_ECHO_LENGTH = 120
# 打断表情包使用的分类名，与 assets/memes/ 下的子目录同名。
INTERRUPT_CATEGORY = "interrupt"

# 冷却表超过该规模时做一次惰性清理，防止长期运行后无界增长。
_COOLDOWN_PRUNE_SIZE = 2000


@dataclass(frozen=True)
class EchoAction:
    """一次复读判定的结果：kind=echo 发文本，kind=interrupt 发对应分类表情包。"""

    kind: str
    text: str = ""
    category: str = ""


def normalize_text(text) -> str:
    """折叠空白得到比较用的文本；富媒体段（CQ 码）与非字符串一律视为不可复读。"""
    if not isinstance(text, str):
        return ""
    if "[CQ:" in text:
        # 图片/语音/表情等 CQ 段无法复述成纯文本，直接放弃。
        return ""
    return " ".join(text.split())


def _tail_streak(window: deque) -> tuple[str, int]:
    """窗口末尾连续相同文本的（文本, 不同发送者数）。

    中间插入任何不同文本都会截断连续段：只有「干净地刷同一句」才算复读。
    """
    if not window:
        return "", 0
    target = window[-1][0]
    senders = set()
    for text, sender, _moment in reversed(window):
        if text != target:
            break
        senders.add(sender)
    return target, len(senders)


class EchoEngine:
    """按群隔离的复读判定引擎；时钟与随机源可注入，便于离线测试。"""

    def __init__(
        self,
        *,
        window_size: int = WINDOW_SIZE,
        repeat_threshold: int = REPEAT_THRESHOLD,
        interrupt_threshold: int = INTERRUPT_THRESHOLD,
        interrupt_probability: float = INTERRUPT_PROBABILITY,
        cooldown_seconds: float = ECHO_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.window_size = window_size
        self.repeat_threshold = repeat_threshold
        self.interrupt_threshold = interrupt_threshold
        self.interrupt_probability = interrupt_probability
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._rng = rng
        self._windows: dict[str, deque] = {}
        self._echoed_at: dict[tuple[str, str], float] = {}
        self._interrupted_at: dict[tuple[str, str], float] = {}

    def observe(self, group_id: str, text: str, sender_id: str, *,
                now: float | None = None) -> EchoAction | None:
        """记录一条群消息；命中复读/打断条件时返回动作，否则返回 None。"""
        group = str(group_id or "").strip()
        normalized = normalize_text(text)
        if not group or not normalized or len(normalized) > MAX_ECHO_LENGTH:
            return None
        sender = str(sender_id or "").strip() or "unknown"
        moment = self._clock() if now is None else now

        window = self._windows.get(group)
        if window is None:
            window = deque(maxlen=self.window_size)
            self._windows[group] = window
        window.append((normalized, sender, moment))

        streak_text, distinct = _tail_streak(window)
        if streak_text != normalized:
            return None
        key = (group, normalized)
        if (distinct >= self.interrupt_threshold
                and self._cooled(self._interrupted_at, key, moment)
                and self._rng() < self.interrupt_probability):
            self._record(self._interrupted_at, key, moment)
            return EchoAction(kind="interrupt", text=normalized,
                              category=INTERRUPT_CATEGORY)
        if distinct >= self.repeat_threshold and self._cooled(self._echoed_at, key, moment):
            self._record(self._echoed_at, key, moment)
            return EchoAction(kind="echo", text=normalized)
        return None

    def reset(self, group_id: str | None = None) -> None:
        """清空状态；只传群标识时仅重置该群（测试与热重载用）。"""
        if group_id is None:
            self._windows.clear()
            self._echoed_at.clear()
            self._interrupted_at.clear()
            return
        group = str(group_id)
        self._windows.pop(group, None)
        for table in (self._echoed_at, self._interrupted_at):
            for key in [k for k in table if k[0] == group]:
                table.pop(key, None)

    def _cooled(self, table: dict[tuple[str, str], float], key: tuple[str, str],
                moment: float) -> bool:
        last = table.get(key)
        return last is None or moment - last >= self.cooldown_seconds

    def _record(self, table: dict[tuple[str, str], float], key: tuple[str, str],
                moment: float) -> None:
        table[key] = moment
        if len(table) > _COOLDOWN_PRUNE_SIZE:
            deadline = moment - self.cooldown_seconds
            for stale in [k for k, at in table.items() if at < deadline]:
                table.pop(stale, None)


_default_engine: EchoEngine | None = None


def get_engine() -> EchoEngine:
    """进程级单例：滑动窗口必须跨消息保留，不能每次调用重建。"""
    global _default_engine
    if _default_engine is None:
        _default_engine = EchoEngine()
    return _default_engine


def reset_engine() -> None:
    """丢弃单例（测试隔离用）。"""
    global _default_engine
    _default_engine = None
