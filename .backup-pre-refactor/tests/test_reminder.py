"""提醒时区 / 去重 / 按群发送离线测试。"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import reminder
from config import REMIND_BEFORE_HOURS, TZ_NAME

TZ = ZoneInfo(TZ_NAME)


def _contest(cid=1, group="g1", deadline="2026-11-01 10:30", ctime="2026-11-05 09:00"):
    return {
        "id": cid,
        "group_id": group,
        "name": "校赛",
        "reg_deadline": deadline,
        "contest_time": ctime,
        "mode": "线上",
        "location": "机房",
    }


def test_parse_time_uses_beijing_tz():
    dt = reminder._parse_time("2026-11-01 10:30")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(hours=8)
    assert reminder._parse_time("") is None
    assert reminder._parse_time("下周三") is None


def test_build_reminders_window():
    now = datetime(2026, 11, 1, 0, 0, tzinfo=TZ)
    # deadline 10:30 在 24h 内 → 应提醒
    contests = [_contest(1, "g1", deadline="2026-11-01 10:30")]
    items = reminder.build_reminders(contests, now)
    assert len(items) == 1
    assert items[0]["remind_type"] == "reg_deadline"
    assert items[0]["group_id"] == "g1"


def test_build_reminders_past_not_included():
    now = datetime(2026, 11, 1, 0, 0, tzinfo=TZ)
    contests = [_contest(1, "g1", deadline="2026-10-31 10:30")]
    assert reminder.build_reminders(contests, now) == []


def test_build_reminders_beyond_window():
    now = datetime(2026, 11, 1, 0, 0, tzinfo=TZ)
    # 48h 后，超出 REMIND_BEFORE_HOURS(24h)
    future = (now + timedelta(hours=REMIND_BEFORE_HOURS + 2)).strftime("%Y-%m-%d %H:%M")
    contests = [_contest(1, "g1", deadline=future)]
    assert reminder.build_reminders(contests, now) == []


def test_build_reminders_skips_missing_time():
    now = datetime(2026, 11, 1, 0, 0, tzinfo=TZ)
    contests = [_contest(1, "g1", deadline="")]
    assert reminder.build_reminders(contests, now) == []


def test_build_reminders_both_types():
    now = datetime(2026, 11, 1, 0, 0, tzinfo=TZ)
    contests = [
        _contest(1, "g1", deadline="2026-11-01 10:00", ctime="2026-11-01 12:00")
    ]
    items = reminder.build_reminders(contests, now)
    assert {i["remind_type"] for i in items} == {"reg_deadline", "contest_time"}


# ---------- check_reminders：发送成功才标记、失败重试、按群 ----------


def _soon_deadline(hours: int = 10) -> str:
    """返回距真实当前时间 hours 小时后的时间文本（保证落在提醒窗口内）。"""
    return (datetime.now(TZ) + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")


async def test_check_reminders_marks_only_on_success(monkeypatch):
    contests = [_contest(1, "g1", deadline=_soon_deadline(10))]
    monkeypatch.setattr(reminder, "list_contests", lambda: contests)
    monkeypatch.setattr(reminder, "has_reminded", lambda *k: False)
    marked = []
    monkeypatch.setattr(reminder, "mark_reminded", lambda *k: marked.append(k) or True)

    async def fail_send(group_id, text):
        return False

    sent = await reminder.check_reminders(fail_send)
    assert sent == 0
    assert marked == []  # 发送失败不标记，留待下次重试


async def test_check_reminders_success_path(monkeypatch):
    contests = [_contest(1, "g1", deadline=_soon_deadline(10))]
    monkeypatch.setattr(reminder, "list_contests", lambda: contests)
    monkeypatch.setattr(reminder, "has_reminded", lambda *k: False)
    marked = []
    monkeypatch.setattr(reminder, "mark_reminded", lambda *k: marked.append(k) or True)

    async def ok_send(group_id, text):
        return True

    sent = await reminder.check_reminders(ok_send)
    assert sent == 1
    assert len(marked) == 1


async def test_check_reminders_skips_old_groupless(monkeypatch):
    contests = [_contest(1, "", deadline=_soon_deadline(10))]
    monkeypatch.setattr(reminder, "list_contests", lambda: contests)

    async def ok_send(group_id, text):
        raise AssertionError("不应发送未归属群记录")

    sent = await reminder.check_reminders(ok_send)
    assert sent == 0


async def test_check_reminders_skips_reminded(monkeypatch):
    contests = [_contest(1, "g1", deadline=_soon_deadline(10))]
    monkeypatch.setattr(reminder, "list_contests", lambda: contests)
    monkeypatch.setattr(reminder, "has_reminded", lambda *k: True)
    marked = []
    monkeypatch.setattr(reminder, "mark_reminded", lambda *k: marked.append(k) or True)

    async def ok_send(group_id, text):
        raise AssertionError("已提醒过的不应再次发送")

    sent = await reminder.check_reminders(ok_send)
    assert sent == 0
    assert marked == []
