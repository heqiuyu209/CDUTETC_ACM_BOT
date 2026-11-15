"""零 Token 复读机离线测试：滑动窗口、复读冷却与打断概率。

覆盖需求点：3 个不同群友同句触发、同一人刷屏不触发、10 分钟内不重复复读、
5 个不同群友可走打断分支、空文本/CQ 段不参与、群之间互不干扰。
"""

import pytest

from cdutetc import chat_fun


class FakeClock:
    """可手动推进的单调时钟，用于验证冷却窗口。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


def _engine(clock, *, rng=lambda: 0.99, **kwargs):
    """默认随机源取 0.99：不触发打断，只验证复读主干。"""
    return chat_fun.EchoEngine(clock=clock, rng=rng, **kwargs)


def _echo_text(action):
    assert action is not None and action.kind == "echo"
    return action.text


def test_three_distinct_members_echo(clock):
    """3 个不同群友发同一句 → 机器人复读原文本，且不调模型。"""
    engine = _engine(clock)
    assert engine.observe("g1", "破防了", "m1") is None
    assert engine.observe("g1", "破防了", "m2") is None
    action = engine.observe("g1", "破防了", "m3")
    assert _echo_text(action) == "破防了"


def test_same_member_spam_not_echo(clock):
    """同一个人刷 3 次不算复读：不同发送者数才是判据。"""
    engine = _engine(clock)
    for _ in range(3):
        assert engine.observe("g1", "破防了", "m1") is None
    # 第 4 条仍是同一人：不同发送者只有 1 个，依旧不触发。
    assert engine.observe("g1", "破防了", "m1") is None


def test_echo_not_repeated_within_ten_minutes(clock):
    """同一句话 10 分钟内只复读一次，避免机器人与群友对喊。"""
    engine = _engine(clock)
    engine.observe("g1", "破防了", "m1")
    engine.observe("g1", "破防了", "m2")
    assert _echo_text(engine.observe("g1", "破防了", "m3")) == "破防了"

    assert engine.observe("g1", "破防了", "m4") is None
    clock.advance(chat_fun.ECHO_COOLDOWN_SECONDS - 1)
    assert engine.observe("g1", "破防了", "m5") is None
    # 冷却到期后可以再次复读。
    clock.advance(2)
    assert _echo_text(engine.observe("g1", "破防了", "m6")) == "破防了"


def test_five_members_trigger_interrupt(clock):
    """连续 5 个不同群友复读且随机命中 → 发 interrupt 表情包打断。"""
    engine = _engine(clock, rng=lambda: 0.0)
    engine.observe("g1", "破防了", "m1")
    engine.observe("g1", "破防了", "m2")
    assert _echo_text(engine.observe("g1", "破防了", "m3")) == "破防了"
    assert engine.observe("g1", "破防了", "m4") is None
    action = engine.observe("g1", "破防了", "m5")
    assert action is not None
    assert action.kind == "interrupt"
    assert action.category == chat_fun.INTERRUPT_CATEGORY


def test_interrupt_probability_gate(clock):
    """15% 概率门：低于阈值命中，等于阈值不命中。"""
    hit = _engine(clock, rng=lambda: 0.14)
    for sender in ("m1", "m2", "m3", "m4"):
        hit.observe("g1", "破防了", sender)
    action = hit.observe("g1", "破防了", "m5")
    assert action is not None and action.kind == "interrupt"

    other_clock = FakeClock()
    miss = _engine(other_clock, rng=lambda: 0.15)
    for sender in ("m1", "m2", "m3", "m4"):
        miss.observe("g1", "破防了", sender)
    assert miss.observe("g1", "破防了", "m5") is None


def test_window_keeps_last_five(clock):
    """滑动窗口长度固定为 5，旧消息自然淘汰。"""
    engine = _engine(clock)
    for index in range(8):
        engine.observe("g1", f"话{index}", f"m{index}")
    window = engine._windows["g1"]
    assert len(window) == chat_fun.WINDOW_SIZE
    assert [item[0] for item in window] == ["话3", "话4", "话5", "话6", "话7"]


def test_blank_and_media_text_ignored(clock):
    """空文本与带 CQ 段的富媒体消息不参与复读，也不污染窗口。"""
    engine = _engine(clock)
    assert engine.observe("g1", "", "m1") is None
    assert engine.observe("g1", "   ", "m2") is None
    assert engine.observe("g1", "[CQ:image,file=a.jpg]", "m3") is None
    assert "g1" not in engine._windows

    engine.observe("g1", "破防了", "m1")
    engine.observe("g1", "破防了", "m2")
    assert _echo_text(engine.observe("g1", "破防了", "m3")) == "破防了"


def test_whitespace_normalized(clock):
    """首尾与换行差异视为同一句；中间多一个空格则是另一句。"""
    engine = _engine(clock)
    engine.observe("g1", "  破防了  ", "m1")
    engine.observe("g1", "破防了\n", "m2")
    assert _echo_text(engine.observe("g1", "破防了", "m3")) == "破防了"

    other = _engine(FakeClock())
    other.observe("g2", "破防了", "m1")
    other.observe("g2", "破防了", "m2")
    assert other.observe("g2", "破防 了", "m3") is None


def test_other_text_breaks_streak(clock):
    """中间插入别的话会截断连续段，不算复读。"""
    engine = _engine(clock)
    engine.observe("g1", "破防了", "m1")
    engine.observe("g1", "破防了", "m2")
    engine.observe("g1", "别的话题", "m3")
    assert engine.observe("g1", "破防了", "m4") is None


def test_long_text_ignored(clock):
    """超长文本不复读，防止把转发长文刷一遍。"""
    engine = _engine(clock)
    long_text = "啊" * (chat_fun.MAX_ECHO_LENGTH + 1)
    for sender in ("m1", "m2", "m3"):
        assert engine.observe("g1", long_text, sender) is None


def test_groups_isolated(clock):
    """不同群的窗口互不影响：g2 的复读不能把 g1 的进度补满。"""
    engine = _engine(clock)
    engine.observe("g1", "破防了", "m1")
    engine.observe("g2", "破防了", "m2")
    # g1 只有 2 条同句：g2 里的复读不能计入 g1 的连续段。
    assert engine.observe("g1", "破防了", "m3") is None
    engine.observe("g2", "破防了", "m4")
    assert _echo_text(engine.observe("g2", "破防了", "m5")) == "破防了"


def test_reset_group_clears_state(clock):
    """reset(group) 只清该群状态。"""
    engine = _engine(clock)
    engine.observe("g1", "破防了", "m1")
    engine.observe("g2", "破防了", "m2")
    engine.reset("g1")
    assert "g1" not in engine._windows
    assert "g2" in engine._windows
    engine.reset()
    assert engine._windows == {}


def test_engine_singleton(monkeypatch):
    """进程级单例：窗口必须跨消息保留。"""
    chat_fun.reset_engine()
    try:
        first = chat_fun.get_engine()
        assert chat_fun.get_engine() is first
        chat_fun.reset_engine()
        assert chat_fun.get_engine() is not first
    finally:
        chat_fun.reset_engine()
