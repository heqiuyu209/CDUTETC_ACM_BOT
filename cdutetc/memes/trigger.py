"""触发与选取层：关键词分类、场景时机、概率/冷却闸门与素材挑选。"""

from __future__ import annotations

import logging
import random
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from datetime import time as day_time
from pathlib import Path
from zoneinfo import ZoneInfo

import cdutetc.memes as _facade
from cdutetc import config
from cdutetc.memes.rules import (
    CATEGORY_QUESTION,
    CATEGORY_SLACKING,
    GROUP_COOLDOWN_SECONDS,
    LATE_NIGHT_KEYWORDS,
    LATE_NIGHT_WINDOWS,
    MEAL_KEYWORDS,
    MEAL_WINDOWS,
    QUESTION_MAX_CHARS,
    QUESTION_WINDOW,
    RECENT_SENT_LIMIT,
    SCENE_COOLDOWN_SECONDS,
    TRIGGER_PROBABILITY,
)

logger = logging.getLogger("memes")

def pick_meme(category: str, *, root: Path | None = None,
              choice: Callable[..., str] = random.choice, exclude: Iterable[str] = (),
              prefer_text: str = "") -> str:
    """取一张素材的相对路径；分类无素材时返回空串。

    选图顺序：先在「未被近期发过」的素材里挑，再看聊天文本是否命中文件名前缀
    （``AC_xxx.png`` 配「终于 AC 了」），都没命中才随机抽。
    """
    names = _facade.list_memes(category, root=root)
    if not names:
        return ""
    banned = {str(item) for item in exclude}
    usable = [name for name in names if name not in banned] or names
    if prefer_text:
        matched = [
            name for name in usable
            if _facade.filename_tag(name) and _facade.text_hits_tag(prefer_text, _facade.filename_tag(name))
        ]
        if matched:
            return choice(matched)
    return choice(usable)
@dataclass(frozen=True)
class MemeTrigger:
    """一次关键词触发的结论：分类 + 相对路径 + 公网地址。"""

    category: str
    relative_path: str
    url: str


def is_within(window: tuple[day_time, day_time], moment: day_time) -> bool:
    """时刻是否落在时间窗内；支持跨午夜窗（如 23:30-02:00）。"""
    start, end = window
    if start <= end:
        return start <= moment < end
    return moment >= start or moment < end


def slacking_scene(text: str, moment: datetime) -> bool:
    """饭点喊饿、深夜喊困才算摸鱼场景；其他时间不主动打扰。"""
    if not isinstance(text, str) or not text.strip():
        return False
    current = moment.timetz().replace(tzinfo=None)
    if (any(is_within(window, current) for window in MEAL_WINDOWS)
            and any(word in text for word in MEAL_KEYWORDS)):
        return True
    return bool(any(is_within(window, current) for window in LATE_NIGHT_WINDOWS)
                and any(word in text for word in LATE_NIGHT_KEYWORDS))


def is_short_question(text: str) -> bool:
    """短句 + 带问号：起哄式疑问，才值得甩一张问号图。"""
    clean = " ".join(str(text or "").split())
    if not clean or len(clean) > QUESTION_MAX_CHARS:
        return False
    return "?" in clean or "？" in clean


class MemeEngine:
    """发图决策中枢：关键词概率、场景触发、防重队列与素材目录均可注入。"""

    def __init__(
        self,
        *,
        probability: float = TRIGGER_PROBABILITY,
        cooldown_seconds: float = GROUP_COOLDOWN_SECONDS,
        scene_cooldown_seconds: float = SCENE_COOLDOWN_SECONDS,
        recent_limit: int = RECENT_SENT_LIMIT,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
        root: Path | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.probability = probability
        self.cooldown_seconds = cooldown_seconds
        self.scene_cooldown_seconds = scene_cooldown_seconds
        self.recent_limit = recent_limit
        self._clock = clock
        self._rng = rng
        self._root = root
        self._now = now_provider or (lambda: datetime.now(ZoneInfo(config.TZ_NAME)))
        self._last_at: dict[str, float] = {}
        self._scene_at: dict[str, float] = {}
        self._recent: dict[str, deque[str]] = {}
        self._window: dict[str, deque[str]] = {}

    # ---------------- 关键词触发 ----------------
    def should_trigger(self, group_id: str, text: str, *,
                       now: float | None = None) -> str:
        """命中关键词且通过冷却与概率时返回分类名，否则返回空串。

        冷却先于概率判断：冷却期内的消息既不掷骰子也不刷新冷却时间。
        """
        category = _facade.match_category(text)
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

    # ---------------- 场景触发 ----------------
    def remember_text(self, group_id: str, text: str) -> None:
        """把群聊文本塞进内存滑窗：只留最近几条短句，供连续问号判定。"""
        group = str(group_id or "").strip()
        clean = " ".join(str(text or "").split())
        if not group or not clean:
            return
        window = self._window.setdefault(group, deque(maxlen=QUESTION_WINDOW))
        window.append(clean)

    def recent_texts(self, group_id: str) -> list[str]:
        """当前群的内存滑窗文本（旧 → 新）。"""
        return list(self._window.get(str(group_id or "").strip(), ()))

    def scene_trigger(self, group_id: str, text: str, *, now: float | None = None,
                      moment: datetime | None = None,
                      remember: bool = True) -> str:
        """场景触发：连续问号 → question，饭点/深夜关键词 → slacking。

        默认会先记一次文本滑窗（调用方不要再重复 remember_text）；
        调用方已统一维护滑窗时传 ``remember=False``，避免同一条消息进窗两次。
        """
        group = str(group_id or "").strip()
        if not group:
            return ""
        if remember:
            self.remember_text(group, text)
        category = ""
        recent = self.recent_texts(group)[-2:]
        if len(recent) == 2 and all(is_short_question(item) for item in recent):
            category = CATEGORY_QUESTION
        else:
            current = self._now() if moment is None else moment
            if slacking_scene(text, current):
                category = CATEGORY_SLACKING
        if not category:
            return ""
        stamp = self._clock() if now is None else now
        last = self._scene_at.get(group)
        if last is not None and stamp - last < self.scene_cooldown_seconds:
            return ""
        self._scene_at[group] = stamp
        return category

    # ---------------- 选图与防重 ----------------
    def recent_sent(self, group_id: str) -> list[str]:
        """该群最近发过的素材相对路径（旧 → 新）。"""
        return list(self._recent.get(str(group_id or "").strip(), ()))

    def record_sent(self, group_id: str, relative_path: str) -> None:
        """登记一张刚发出的图，超出队列长度自动淘汰最旧的。"""
        group = str(group_id or "").strip()
        relative = str(relative_path or "").strip()
        if not group or not relative:
            return
        sent = self._recent.setdefault(group, deque(maxlen=self.recent_limit))
        sent.append(relative)

    def recall_last_sent(self, group_id: str) -> str:
        """取该群最近一次发出的素材路径（/删图 的目标），不发不返回。"""
        sent = self._recent.get(str(group_id or "").strip())
        return sent[-1] if sent else ""

    def forget_sent(self, group_id: str, relative_path: str) -> None:
        """把一张已被删除的图从防重队列里摘掉。"""
        sent = self._recent.get(str(group_id or "").strip())
        if not sent:
            return
        remaining = [item for item in sent if item != str(relative_path or "").strip()]
        self._recent[str(group_id).strip()] = deque(remaining, maxlen=self.recent_limit)

    def resolve(self, category: str, *, group_id: str = "",
                text: str = "") -> MemeTrigger | None:
        """把分类解析成可发送的（相对路径, 公网地址）；任一步缺失即返回 None。

        ``group_id`` 用于避开该群最近发过的图，``text`` 用于优先命中标签前缀。
        """
        group = str(group_id or "").strip()
        exclude = self.recent_sent(group) if group else ()
        relative = pick_meme(category, root=self._root, exclude=exclude,
                             prefer_text=text)
        if not relative:
            return None
        url = _facade.public_url(relative)
        if not url:
            return None
        return MemeTrigger(category=category, relative_path=relative, url=url)

    def reset(self, group_id: str | None = None) -> None:
        if group_id is None:
            self._last_at.clear()
            self._scene_at.clear()
            self._recent.clear()
            self._window.clear()
            return
        group = str(group_id)
        self._last_at.pop(group, None)
        self._scene_at.pop(group, None)
        self._recent.pop(group, None)
        self._window.pop(group, None)


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
