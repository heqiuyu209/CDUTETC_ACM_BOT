"""旧比赛真实 SQLite 投递：跨扫描领取、成功历史与投影故障隔离。"""

import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

import config
import database
import main
import reminder

NOW = datetime(2026, 11, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
TARGET = "2026-11-01 23:59"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite"
    monkeypatch.setattr(database, "DB_PATH", str(path))
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"123456"})
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    database.init_db()
    yield path


def legacy(name="旧比赛", group="123456"):
    cid = database.save_contest(group, {"name": name, "reg_deadline": TARGET})
    return cid, database.delivery_dedup_key(group, "legacy_contest", cid, "reg_deadline", TARGET)


async def test_concurrent_scans_claim_same_legacy_only_once(db):
    _, key = legacy()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def sender(gid, text):
        calls.append((gid, text))
        entered.set()
        await release.wait()
        return True
    first = asyncio.create_task(reminder.check_reminders(sender, now=NOW))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert await reminder.check_reminders(sender, now=NOW) == 0
    finally:
        release.set()
    assert await asyncio.wait_for(first, timeout=2) == 1
    assert len(calls) == 1
    row = database.get_delivery_by_key(key)
    assert (row["status"], row["attempts"]) == ("sent", 1)


async def test_existing_old_success_history_is_never_resent(db):
    cid, key = legacy()
    database.mark_reminded("123456", cid, "reg_deadline", TARGET)
    sender = AsyncMock(return_value=True)
    assert await reminder.check_reminders(sender, now=NOW) == 0
    row = database.get_delivery_by_key(key)
    assert row["status"] == "sent" and row["attempts"] == 0
    database.init_db()
    assert await reminder.check_reminders(sender, now=NOW) == 0
    sender.assert_not_awaited()


async def test_old_projection_trigger_does_not_replay_or_block_healthy_collection(db, monkeypatch):
    cid, key = legacy()
    healthy = database.create_collection("123456", "健康征集", deadline=TARGET)
    with database._connect() as conn:
        conn.execute("""CREATE TRIGGER reject_legacy_projection BEFORE INSERT ON reminders
                        BEGIN SELECT RAISE(ABORT, 'legacy reminder receipt rejected'); END""")
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(reminder, "datetime", Clock)
    client = SimpleNamespace(send_group_msg=AsyncMock(return_value=True))
    bot = main.ACMBot(client)
    await bot._remind_job()
    await bot._remind_job()
    assert client.send_group_msg.await_count == 2
    assert database.get_delivery_by_key(key)["status"] == "sent"
    assert database.has_reminded("123456", cid, "reg_deadline", TARGET)
    assert database.get_delivery_by_key(database.delivery_dedup_key(
        "123456", "collection", healthy, "deadline", TARGET))["status"] == "sent"
    with database._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0
    restart = subprocess.run([
        sys.executable, "-c",
        "import asyncio,json,sys; from datetime import datetime; from zoneinfo import ZoneInfo; "
        "import database,config,reminder; database.DB_PATH=sys.argv[1]; "
        "config.ALLOWED_GROUPS={'123456'}; database.init_db(); "
        "sender=__import__('unittest.mock',fromlist=['AsyncMock']).AsyncMock(return_value=True); "
        "sent=asyncio.run(reminder.check_reminders(sender,now=datetime(2026,11,1,12,3,tzinfo=ZoneInfo('Asia/Shanghai')))); "
        "print(json.dumps({'sent':sent,'calls':sender.await_count}))",
        str(db),
    ], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=True, timeout=10)
    assert json.loads(restart.stdout) == {"sent": 0, "calls": 0}


async def test_queue_projection_failure_also_preserves_success(db):
    _, key = legacy()
    with database._connect() as conn:
        conn.execute("""CREATE TRIGGER reject_queue_projection BEFORE UPDATE OF status ON reminder_deliveries
                        WHEN NEW.status = 'sent' BEGIN SELECT RAISE(ABORT, 'queue projection rejected'); END""")
    sender = AsyncMock(return_value=True)
    assert await reminder.check_reminders(sender, now=NOW) == 1
    assert await reminder.check_reminders(sender, now=NOW + timedelta(minutes=3)) == 0
    sender.assert_awaited_once()
    assert database.get_delivery_by_key(key)["status"] == "sent"


async def test_invalid_plan_projection_failure_does_not_block_healthy_objects(db, monkeypatch):
    cid, key = legacy()
    database.ensure_delivery("123456", "legacy_contest", cid, "reg_deadline", TARGET)
    database.delete_contest("123456", cid)
    database.create_collection("123456", "健康征集", deadline=TARGET)
    with database._connect() as conn:
        conn.execute("""CREATE TRIGGER reject_cancelled_projection BEFORE UPDATE OF status ON reminder_deliveries
                        WHEN NEW.obj_type = 'legacy_contest' AND NEW.status = 'cancelled'
                        BEGIN SELECT RAISE(ABORT, 'cancel projection rejected'); END""")
    sender = AsyncMock(return_value=True)
    assert await reminder.check_reminders(sender, now=NOW) == 0
    assert await reminder.check_general_reminders(sender, now=NOW) == 1
    sender.assert_awaited_once()


@pytest.mark.parametrize("failure", ["exception", "timeout", "no_receipt"])
async def test_failures_retry_with_bound_and_do_not_block_later_legacy(db, monkeypatch, failure):
    cid, key = legacy("坏比赛")
    good, _ = legacy("健康比赛")
    monkeypatch.setattr(reminder, "_DELIVERY_SEND_TIMEOUT_SECONDS", .01)
    async def sender(gid, text):
        if "坏比赛" in text:
            if failure == "exception":
                raise RuntimeError("synthetic sender failure")
            if failure == "timeout":
                await asyncio.Event().wait()
            return False
        return True
    assert await reminder.check_reminders(sender, now=NOW, max_attempts=2) == 1
    assert await reminder.check_reminders(sender, now=NOW, max_attempts=2) == 0
    assert await reminder.check_reminders(sender, now=NOW, max_attempts=2) == 0
    row = database.get_delivery_by_key(key)
    assert (row["status"], row["attempts"]) == ("failed", 2)
    assert not database.has_reminded("123456", cid, "reg_deadline", TARGET)
    assert database.has_reminded("123456", good, "reg_deadline", TARGET)


async def test_group_removal_skips_legacy_without_consuming_attempts(db, monkeypatch):
    _, key = legacy()
    monkeypatch.setattr(config, "ALLOWED_GROUPS", set())
    sender = AsyncMock(return_value=True)
    assert await reminder.check_reminders(sender, now=NOW) == 0
    assert database.get_delivery_by_key(key) is None
    sender.assert_not_awaited()


async def test_reschedule_before_send_invalidates_claim(db, monkeypatch):
    cid, key = legacy()
    original = reminder.claim_delivery
    def claim_then_change(*args, **kwargs):
        token = original(*args, **kwargs)
        with database._connect() as conn:
            conn.execute("UPDATE contests SET reg_deadline = '2026-11-02 23:59' WHERE id = ?", (cid,))
        return token
    monkeypatch.setattr(reminder, "claim_delivery", claim_then_change)
    sender = AsyncMock(return_value=True)
    assert await reminder.check_reminders(sender, now=NOW) == 0
    sender.assert_not_awaited()
    assert database.get_delivery_by_key(key)["status"] == "cancelled"


@pytest.mark.parametrize("stored, claimed", [
    ("2026-11-01 9:00", "2026-11-01 09:00"),
    ("2026-11-01 09:00", "2026-11-1 9:0"),
    ("2026/11/1 9:0", "2026-11-01 09:00"),
    ("2026-11-01 09:00:00", "2026-11-01 09:00"),
])
def test_legacy_claim_and_presend_check_compare_actual_time(db, stored, claimed):
    now = datetime(2026, 11, 1, 8, tzinfo=ZoneInfo("Asia/Shanghai"))
    cid = database.save_contest("123456", {"name": "等价时刻", "contest_time": "2026-11-01 09:00"})
    # 模拟旧格式业务行；不能只依赖启动时迁移或新写入时补零。
    with database._connect() as conn:
        conn.execute("UPDATE contests SET contest_time = ? WHERE id = ?", (stored, cid))
    token = database.claim_delivery("123456", "legacy_contest", cid, "contest_time", claimed,
                                    now=now, allowed_groups={"123456"})
    assert token is not None
    key = database.delivery_dedup_key("123456", "legacy_contest", cid, "contest_time", claimed)
    assert database.validate_delivery_claim(key, token, now=now, allowed_groups={"123456"})
    with database._connect() as conn:
        conn.execute("UPDATE contests SET contest_time = '2026-11-01 09:00' WHERE id = ?", (cid,))
    assert database.validate_delivery_claim(key, token, now=now, allowed_groups={"123456"})
    with database._connect() as conn:
        conn.execute("UPDATE contests SET contest_time = '2026-11-01 09:00:01' WHERE id = ?", (cid,))
    assert not database.validate_delivery_claim(key, token, now=now, allowed_groups={"123456"})
    assert database.get_delivery_by_key(key)["status"] == "cancelled"
