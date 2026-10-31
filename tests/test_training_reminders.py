"""周期课程的真实 SQLite 实例投递、失效校验、群隔离与重启去重。"""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

import commands
import config
import database
import main
import reminder

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 1, 18, 59, tzinfo=TZ)
TARGET = "2026-11-01 19:00"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "training.sqlite"
    monkeypatch.setattr(database, "DB_PATH", str(path))
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"123456", "234567"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"654321"})
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    monkeypatch.setattr(reminder, "REMIND_BEFORE_HOURS", 24)
    database.init_db()
    yield path


def training(group="123456", **values):
    info = {"title": "算法训练", "weekday": "周日", "start_time": "19:00", "end_time": "21:00",
            "location": "B201", "needs_laptop": True, "software": "IDE", "device_req": "充电器", **values}
    tid = database.create_training_session(group, info, operator="654321")
    return tid, database.delivery_dedup_key(group, "training_session", tid, "start_time", TARGET)


async def test_course_command_enters_actual_main_reminder_job(db, monkeypatch):
    reply = commands.cmd_training_add("123456", "654321", "训练 周日 19:00-21:00 B201")
    assert "已添加课程" in reply[0]["text"]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(reminder, "datetime", Clock)
    client = SimpleNamespace(send_group_msg=AsyncMock(return_value=True))
    bot = main.ACMBot(client)
    await bot._remind_job()
    client.send_group_msg.assert_awaited_once()
    group, text = client.send_group_msg.await_args.args
    assert group == "123456" and TARGET in text and "B201" in text
    await bot._remind_job()
    assert client.send_group_msg.await_count == 1


async def test_next_week_has_new_identity_but_same_week_and_restart_are_deduplicated(db):
    tid, key = training()
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW) == 1
    assert await reminder.check_general_reminders(sender, now=NOW) == 0
    database.init_db()
    assert await reminder.check_general_reminders(sender, now=NOW) == 0
    assert await reminder.check_general_reminders(sender, now=NOW + timedelta(days=7)) == 1
    rows = database.list_deliveries("123456")
    assert {r["target_time"] for r in rows} == {TARGET, "2026-11-08 19:00"}
    assert all(r["obj_id"] == tid and r["status"] == "sent" for r in rows)
    restart = subprocess.run([
        sys.executable, "-c",
        "import asyncio,json,sys; from datetime import datetime; from zoneinfo import ZoneInfo; "
        "import database,config,reminder; database.DB_PATH=sys.argv[1]; "
        "config.ALLOWED_GROUPS={'123456'}; database.init_db(); "
        "sender=__import__('unittest.mock',fromlist=['AsyncMock']).AsyncMock(return_value=True); "
        "sent=asyncio.run(reminder.check_general_reminders(sender,now=datetime(2026,11,8,18,59,tzinfo=ZoneInfo('Asia/Shanghai')))); "
        "print(json.dumps({'sent':sent,'calls':sender.await_count}))", str(db),
    ], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=True, timeout=10)
    assert json.loads(restart.stdout) == {"sent": 0, "calls": 0}


async def test_course_reminders_are_sent_only_to_own_allowed_group(db):
    training()
    training("234567", title="另一群课程")
    training("345678", title="非授权课程")
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW) == 2
    messages = {call.args[0]: call.args[1] for call in sender.await_args_list}
    assert set(messages) == {"123456", "234567"}
    assert "算法训练" in messages["123456"] and "另一群课程" not in messages["123456"]
    assert "另一群课程" in messages["234567"] and "算法训练" not in messages["234567"]


async def test_cancelled_course_cancels_failed_plan(db):
    tid, key = training()
    assert await reminder.check_general_reminders(AsyncMock(return_value=False), now=NOW) == 0
    assert database.cancel_training_session("123456", tid, operator="654321")
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW) == 0
    sender.assert_not_awaited()
    assert database.get_delivery_by_key(key)["status"] == "cancelled"


async def test_reschedule_invalidates_old_instance_and_creates_new_target(db):
    tid, key = training()
    assert await reminder.check_general_reminders(AsyncMock(return_value=False), now=NOW) == 0
    with database._connect() as conn:
        conn.execute("UPDATE training_sessions SET start_time = '20:00' WHERE id = ?", (tid,))
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW) == 1
    assert database.get_delivery_by_key(key)["status"] == "cancelled"
    assert "2026-11-01 20:00" in sender.await_args.args[1]
    with database._connect() as conn:
        conn.execute("UPDATE training_sessions SET start_time = '19:00' WHERE id = ?", (tid,))
    assert await reminder.check_general_reminders(sender, now=NOW) == 1
    assert await reminder.check_general_reminders(sender, now=NOW) == 0
    assert sender.await_count == 2


async def test_weekday_change_replaces_the_old_calendar_instance(db):
    tid, key = training()
    assert await reminder.check_general_reminders(AsyncMock(return_value=False), now=NOW) == 0
    with database._connect() as conn:
        conn.execute("UPDATE training_sessions SET weekday='周一',start_time='18:00' WHERE id=?", (tid,))
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW) == 1
    assert database.get_delivery_by_key(key)["status"] == "cancelled"
    assert "2026-11-02 18:00" in sender.await_args.args[1]


async def test_cancelled_inflight_success_keeps_history_when_course_is_restored(db):
    tid, key = training()
    entered, release = asyncio.Event(), asyncio.Event()
    async def in_flight(*args):
        entered.set()
        await release.wait()
        return True
    worker = asyncio.create_task(reminder.check_general_reminders(in_flight, now=NOW))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        database.cancel_training_session("123456", tid)
        database.reconcile_deliveries(NOW, allowed_groups={"123456"})
    finally:
        release.set()
    assert await asyncio.wait_for(worker, timeout=2) == 1
    assert database.get_delivery_by_key(key)["status"] == "sent"
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW + timedelta(days=7)) == 0
    with database._connect() as conn:
        conn.execute("UPDATE training_sessions SET status='confirmed' WHERE id=?", (tid,))
    assert await reminder.check_general_reminders(sender, now=NOW) == 0
    assert await reminder.check_general_reminders(sender, now=NOW + timedelta(days=7)) == 1
    sender.assert_awaited_once()


async def test_failed_course_send_retries_then_persists_success(db):
    _, key = training()
    assert await reminder.check_general_reminders(AsyncMock(return_value=False), now=NOW) == 0
    assert database.get_delivery_by_key(key)["status"] == "pending"
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW) == 1
    assert await reminder.check_general_reminders(sender, now=NOW) == 0
    assert database.get_delivery_by_key(key)["attempts"] == 2
    sender.assert_awaited_once()


@pytest.mark.parametrize("values, expected", [
    ({"effective_start": TARGET, "effective_end": "2026-11-02 00:00"}, 1),
    ({"effective_start": "2026-11-01 20:00"}, 0),
    ({"effective_end": "2026-11-01 18:59"}, 0),
    ({"effective_end": TARGET}, 1),
    ({"status": "draft"}, 0),
    ({"weekday": "不确定"}, 0),
    ({"start_time": "25:00"}, 0),
])
async def test_effective_range_uses_occurrence_and_invalid_fields_are_not_guessed(db, values, expected):
    training(**values)
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW) == expected
    assert sender.await_count == expected


async def test_utc_clock_and_unpadded_time_produce_same_beijing_identity(db):
    tid, key = training(start_time="19:0", weekday="每星期天")
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=datetime(2026, 11, 1, 10, 59, tzinfo=timezone.utc)) == 1
    assert database.get_delivery_by_key(key)["status"] == "sent"
    with database._connect() as conn:
        conn.execute("UPDATE training_sessions SET start_time='19:00' WHERE id=?", (tid,))
    assert await reminder.check_general_reminders(sender, now=NOW) == 0
    sender.assert_awaited_once()


async def test_cancel_after_claim_before_send_prevents_delivery(db, monkeypatch):
    tid, key = training()
    original = reminder.claim_delivery
    def claim_then_cancel(*args, **kwargs):
        token = original(*args, **kwargs)
        database.cancel_training_session("123456", tid)
        return token
    monkeypatch.setattr(reminder, "claim_delivery", claim_then_cancel)
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW) == 0
    sender.assert_not_awaited()
    assert database.get_delivery_by_key(key)["status"] == "cancelled"


async def test_concurrent_training_scans_deliver_one_instance(db):
    _, key = training()
    entered, release = asyncio.Event(), asyncio.Event()
    sender = AsyncMock()
    async def send(*args):
        entered.set()
        await release.wait()
        return True
    sender.side_effect = send
    first = asyncio.create_task(reminder.check_general_reminders(sender, now=NOW))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert await reminder.check_general_reminders(sender, now=NOW) == 0
    finally:
        release.set()
    assert await asyncio.wait_for(first, timeout=2) == 1
    sender.assert_awaited_once()
    assert database.get_delivery_by_key(key)["attempts"] == 1


async def test_unknown_or_past_schedule_does_not_block_healthy_competition(db):
    training(weekday="下周")
    training(start_time="18:00")
    database.create_competition({"name": "健康比赛", "official_time": TARGET}, status="confirmed")
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW) == 2
    assert all("健康比赛" in call.args[1] for call in sender.await_args_list)
