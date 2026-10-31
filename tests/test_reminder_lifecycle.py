"""提醒状态、跨连接领取、租约恢复及旧去重键迁移的离线回归测试。"""

import asyncio
import json
import re
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

import commands
import config
import database
import main
import reminder

NOW = datetime(2026, 11, 1, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
TARGET = "2026-11-01 23:59"


@pytest.fixture
def file_db(tmp_path, monkeypatch):
    path = tmp_path / "reminders.sqlite"
    connections = []

    def connect():
        conn = sqlite3.connect(path, timeout=5, check_same_thread=False)
        connections.append(conn)
        return conn

    monkeypatch.setattr(database, "_connect", connect)
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2"})
    database.init_db()
    yield path
    for conn in connections:
        conn.close()


def collection():
    cid = database.create_collection("g1", "报名", deadline=TARGET)
    return cid, database.delivery_dedup_key("g1", "collection", cid, "deadline", TARGET)


def claim(cid, now=NOW, **kwargs):
    return database.claim_delivery("g1", "collection", cid, "deadline", TARGET,
                                   now=now, **kwargs)


async def test_closed_collection_never_generates_a_reminder(file_db):
    cid, _ = collection()
    database.set_collection_status(cid, "closed")
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW) == 0
    sender.assert_not_awaited()
    assert database.list_deliveries("g1") == []


async def test_failed_plan_is_cancelled_after_collection_closes(file_db):
    cid, key = collection()
    sender = AsyncMock(return_value=False)
    assert await reminder.check_general_reminders(sender, now=NOW) == 0
    database.set_collection_status(cid, "closed")
    assert await reminder.check_general_reminders(sender, now=NOW) == 0
    assert sender.await_count == 1
    row = database.get_delivery_by_key(key)
    assert (row["status"], row["attempts"]) == ("cancelled", 1)
    assert database.list_pending_deliveries() == []


@pytest.mark.parametrize("change, expected", [
    ("expired", "expired"), ("amended", "cancelled"),
    ("cleared", "cancelled"), ("deleted", "cancelled"),
    ("transferred", "cancelled"),
])
async def test_stale_collection_plans_leave_pending(file_db, change, expected):
    cid, key = collection()
    database.ensure_delivery("g1", "collection", cid, "deadline", TARGET)
    now = NOW
    with database._connect() as conn:
        if change == "expired":
            now = NOW + timedelta(days=1)
        elif change == "amended":
            conn.execute("UPDATE collections SET deadline = ? WHERE id = ?", ("2026-11-05 10:00", cid))
        elif change == "cleared":
            conn.execute("UPDATE collections SET deadline = '' WHERE id = ?", (cid,))
        elif change == "deleted":
            conn.execute("DELETE FROM collections WHERE id = ?", (cid,))
        else:
            conn.execute("UPDATE collections SET group_id = 'g2', deadline = '' WHERE id = ?", (cid,))
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=now) == 0
    sender.assert_not_awaited()
    assert database.get_delivery_by_key(key)["status"] == expected
    assert database.list_pending_deliveries() == []


@pytest.mark.parametrize("kind, field", [
    ("reg_deadline", "official_reg_deadline"), ("contest_time", "official_time"),
])
async def test_competition_amendment_cancels_old_plan_and_sends_new_time(file_db, kind, field, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1"})
    cid = database.create_competition({"name": "校赛", field: TARGET}, status="confirmed")
    database.ensure_delivery("g1", "competition", cid, kind, TARGET)
    old_key = database.delivery_dedup_key("g1", "competition", cid, kind, TARGET)
    new_target = "2026-11-02 10:00"
    database.amend_competition(cid, {field: new_target})
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW) == 1
    assert new_target in sender.await_args.args[1]
    assert TARGET not in sender.await_args.args[1]
    assert database.get_delivery_by_key(old_key)["status"] == "cancelled"
    new_key = database.delivery_dedup_key("g1", "competition", cid, kind, new_target)
    assert database.get_delivery_by_key(new_key)["status"] == "sent"


@pytest.mark.parametrize("change", ["draft", "group_removed", "cleared"])
async def test_invalid_competition_plans_are_cancelled(file_db, change, monkeypatch):
    cid = database.create_competition({"name": "校赛", "official_time": TARGET}, status="confirmed")
    database.ensure_delivery("g1", "competition", cid, "contest_time", TARGET)
    key = database.delivery_dedup_key("g1", "competition", cid, "contest_time", TARGET)
    if change == "group_removed":
        monkeypatch.setattr(config, "ALLOWED_GROUPS", set())
    elif change == "cleared":
        database.amend_competition(cid, {"official_time": ""})
    else:
        with database._connect() as conn:
            conn.execute("UPDATE competitions SET status = 'draft' WHERE id = ?", (cid,))
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW) == 0
    sender.assert_not_awaited()
    assert database.get_delivery_by_key(key)["status"] == "cancelled"


async def test_object_is_rechecked_after_claim_and_before_send(file_db, monkeypatch):
    cid, key = collection()
    original_claim = reminder.claim_delivery

    def close_after_claim(*args, **kwargs):
        token = original_claim(*args, **kwargs)
        database.set_collection_status(cid, "closed")
        return token

    monkeypatch.setattr(reminder, "claim_delivery", close_after_claim)
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW) == 0
    sender.assert_not_awaited()
    row = database.get_delivery_by_key(key)
    assert row["status"] == "cancelled" and row["lease_token"] == ""


async def test_later_item_uses_fresh_time_after_an_await(file_db, monkeypatch):
    collection()
    short_target = "2026-11-01 12:01"
    cid = database.create_collection("g1", "即将截止", deadline=short_target)
    key = database.delivery_dedup_key("g1", "collection", cid, "deadline", short_target)
    current = NOW

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current

    monkeypatch.setattr(reminder, "datetime", Clock)
    calls = []

    async def sender(gid, text):
        nonlocal current
        calls.append(text)
        current += timedelta(minutes=2)
        return True

    assert await reminder.check_general_reminders(sender) == 1
    assert len(calls) == 1 and "即将截止" not in calls[0]
    row = database.get_delivery_by_key(key)
    assert row["status"] == "expired" and row["attempts"] == 0


async def test_sender_exception_exhausts_budget_and_preserves_error(file_db):
    _, key = collection()
    sender = AsyncMock(side_effect=RuntimeError("receipt rejected"))
    for _ in range(4):
        assert await reminder.check_general_reminders(sender, now=NOW, max_attempts=2) == 0
    assert sender.await_count == 2
    row = database.get_delivery_by_key(key)
    assert (row["status"], row["attempts"], row["last_error"]) == ("failed", 2, "receipt rejected")
    assert database.list_pending_deliveries() == []


async def test_sender_timeout_releases_claim_for_retry(file_db, monkeypatch):
    _, key = collection()
    monkeypatch.setattr(reminder, "_DELIVERY_SEND_TIMEOUT_SECONDS", 0.01)

    async def slow_sender(*args):
        await asyncio.Event().wait()

    assert await reminder.check_general_reminders(slow_sender, now=NOW) == 0
    row = database.get_delivery_by_key(key)
    assert (row["status"], row["attempts"], row["last_error"]) == ("pending", 1, "发送超时")
    assert row["lease_token"] == row["lease_until"] == ""
    assert await reminder.check_general_reminders(AsyncMock(return_value=True), now=NOW) == 1


@pytest.mark.parametrize("legacy_result", ["timeout", "exception", "no_receipt"])
async def test_main_job_continues_after_legacy_send_failure(file_db, monkeypatch, legacy_result, onebot_client):
    _, key = collection()
    legacy_id = database.save_contest("g1", {"name": "旧比赛", "reg_deadline": TARGET})
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    monkeypatch.setattr(reminder, "_DELIVERY_SEND_TIMEOUT_SECONDS", 0.01)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(reminder, "datetime", Clock)
    legacy_calls = []

    async def api_send(action, **kwargs):
        if "旧比赛" in kwargs["message"]:
            legacy_calls.append(kwargs["group_id"])
            if legacy_result == "timeout":
                await asyncio.Event().wait()
            if legacy_result == "exception":
                raise RuntimeError("legacy SDK rejected")
            return None
        return {"status": "ok", "retcode": 0, "data": {"message_id": 12345}}

    client = main.ACMBot(onebot_client)
    onebot_client.call_api.side_effect = api_send
    await asyncio.wait_for(client._remind_job(), timeout=2)
    row = database.get_delivery_by_key(key)
    assert (row["status"], row["attempts"]) == ("sent", 1)
    assert legacy_calls == ["g1"]
    assert not database.has_reminded("g1", legacy_id, "reg_deadline", TARGET)
    await asyncio.wait_for(client._remind_job(), timeout=2)
    assert legacy_calls == ["g1", "g1"]
    assert database.get_delivery_by_key(key)["attempts"] == 1


async def test_legacy_callback_exception_does_not_skip_later_legacy_item(file_db, monkeypatch):
    first = database.save_contest("g1", {"name": "发送失败比赛", "reg_deadline": TARGET})
    second = database.save_contest("g1", {"name": "后续比赛", "reg_deadline": TARGET})

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(reminder, "datetime", Clock)

    async def sender(gid, text):
        if "发送失败比赛" in text:
            raise RuntimeError("sender raised")
        return True

    assert await reminder.check_reminders(sender) == 1
    assert not database.has_reminded("g1", first, "reg_deadline", TARGET)
    assert database.has_reminded("g1", second, "reg_deadline", TARGET)


async def test_concurrent_scans_do_not_send_the_same_claim(file_db):
    _, key = collection()
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def sender(gid, text):
        calls.append(gid)
        started.set()
        await release.wait()
        return True

    first = asyncio.create_task(reminder.check_general_reminders(sender, now=NOW))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        assert await reminder.check_general_reminders(sender, now=NOW) == 0
        assert database.get_delivery_by_key(key)["status"] == "sending"
    finally:
        release.set()
    assert await first == 1
    assert calls == ["g1"]
    row = database.get_delivery_by_key(key)
    assert (row["status"], row["attempts"]) == ("sent", 1)


def test_sqlite_claim_is_atomic_across_independent_connections(file_db):
    cid, key = collection()
    barrier = Barrier(2)

    def compete():
        barrier.wait(timeout=2)
        return claim(cid)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(compete) for _ in range(2)]
        tokens = [future.result(timeout=5) for future in futures]
    assert sum(token is not None for token in tokens) == 1
    row = database.get_delivery_by_key(key)
    assert row["status"] == "sending" and row["attempts"] == 1


def test_expired_lease_recovers_and_old_owner_cannot_acknowledge(file_db):
    cid, key = collection()
    old = claim(cid)
    assert old and claim(cid, now=NOW + timedelta(seconds=119)) is None
    new_time = NOW + timedelta(seconds=121)
    new = claim(cid, now=new_time)
    assert new and new != old
    assert not database.validate_delivery_claim(key, old, now=new_time)
    assert not database.finish_delivery(key, old, True, now=new_time)
    assert database.validate_delivery_claim(key, new, now=new_time)
    assert database.finish_delivery(key, new, True, now=new_time)
    row = database.get_delivery_by_key(key)
    assert (row["status"], row["attempts"]) == ("sent", 2)


def test_crash_on_last_attempt_becomes_failed_after_lease_expiry(file_db):
    cid, key = collection()
    assert claim(cid, max_attempts=1)
    database.reconcile_deliveries(NOW + timedelta(seconds=119), max_attempts=1)
    assert database.get_delivery_by_key(key)["status"] == "sending"
    database.reconcile_deliveries(NOW + timedelta(seconds=121), max_attempts=1)
    row = database.get_delivery_by_key(key)
    assert row["status"] == "failed" and row["attempts"] == 1
    assert claim(cid, now=NOW + timedelta(seconds=122), max_attempts=1) is None


async def test_cancelled_worker_recovers_after_lease_expires(file_db):
    _, key = collection()
    started = asyncio.Event()

    async def sender(*args):
        started.set()
        await asyncio.Event().wait()

    worker = asyncio.create_task(reminder.check_general_reminders(sender, now=NOW))
    await asyncio.wait_for(started.wait(), timeout=2)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    assert database.get_delivery_by_key(key)["status"] == "sending"
    assert await reminder.check_general_reminders(AsyncMock(return_value=True), now=NOW + timedelta(seconds=121)) == 1
    assert database.get_delivery_by_key(key)["attempts"] == 2


async def test_sent_dedup_survives_database_reinitialization(file_db):
    _, key = collection()
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW) == 1
    database.init_db()
    assert await reminder.check_general_reminders(sender, now=NOW) == 0
    sender.assert_awaited_once()
    assert database.get_delivery_by_key(key)["status"] == "sent"


async def test_old_schema_and_mixed_keys_preserve_sent_and_max_attempts(file_db):
    cid, key = collection()
    old_key = f"g1:collection:{cid}:deadline:{TARGET}"
    with database._connect() as conn:
        conn.execute("DROP TABLE reminder_deliveries")
        conn.execute("""CREATE TABLE reminder_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL,
            obj_type TEXT NOT NULL, obj_id INTEGER NOT NULL, remind_type TEXT NOT NULL,
            target_time TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending', sent_at TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '', dedup_key TEXT NOT NULL UNIQUE)""")
        for dedup_key, status, attempts, sent_at in [(old_key, "sent", 1, "2026-11-01 12:00:00"), (key, "pending", 2, "")]:
            conn.execute("""INSERT INTO reminder_deliveries
                (group_id, obj_type, obj_id, remind_type, target_time, attempts, status, sent_at, dedup_key)
                VALUES ('g1', 'collection', ?, 'deadline', ?, ?, ?, ?, ?)""",
                (cid, TARGET, attempts, status, sent_at, dedup_key))
    database.init_db()
    database.init_db()
    row = database.get_delivery_by_key(old_key)
    assert row["dedup_key"] == key
    assert (row["status"], row["attempts"], row["sent_at"]) == ("sent", 2, "2026-11-01 12:00:00")
    assert row["lease_token"] == row["lease_until"] == ""
    assert len(database.list_deliveries("g1")) == 1
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW) == 0
    sender.assert_not_awaited()
    with database._connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("""INSERT INTO reminder_deliveries
            (group_id, obj_type, obj_id, remind_type, target_time, dedup_key)
            VALUES ('g1', 'collection', ?, 'deadline', ?, ?)""", (cid, TARGET, old_key))


def test_reminder_status_filters_group_before_limiting_and_shows_terminal_counts(file_db, monkeypatch):
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    for obj_id in range(205):
        database.ensure_delivery("g2", "collection", obj_id, "deadline", TARGET)
    cid, key = collection()
    token = claim(cid)
    assert database.finish_delivery(key, token, False, now=NOW, max_attempts=1)
    monkeypatch.setattr(commands.db, "reconcile_deliveries", lambda **kwargs: None)
    text = commands.cmd_reminder_status("g1", "teacher", "")[0]["text"]
    assert "重试耗尽 1" in text
    assert "已取消 0" in text and "已过期 0" in text
    assert "本群当前无待发送提醒" in text
    pending_id, _ = collection()
    database.ensure_delivery("g1", "collection", pending_id, "deadline", TARGET)
    text = commands.cmd_reminder_status("g1", "teacher", "")[0]["text"]
    assert f"collection#{pending_id}" in text and "待发送 1" in text
    assert "重试耗尽 1" in text


async def test_unsent_collection_resumes_after_reopening_but_sent_history_does_not(file_db):
    cid, key = collection()
    failing = AsyncMock(return_value=False)
    assert await reminder.check_general_reminders(failing, now=NOW) == 0
    database.set_collection_status(cid, "closed")
    database.reconcile_deliveries(NOW, allowed_groups={"g1"})
    assert database.get_delivery_by_key(key)["status"] == "cancelled"
    database.set_collection_status(cid, "open")
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW) == 1
    row = database.get_delivery_by_key(key)
    assert (row["status"], row["attempts"]) == ("sent", 2)
    database.set_collection_status(cid, "closed")
    database.reconcile_deliveries(NOW, allowed_groups={"g1"})
    database.set_collection_status(cid, "open")
    assert await reminder.check_general_reminders(sender, now=NOW) == 0
    sender.assert_awaited_once()
    assert database.get_delivery_by_key(key) == row


async def test_amend_command_reschedule_then_revert_resumes_unsent_target(file_db, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    cid = database.create_competition({"name": "校赛", "official_time": TARGET}, status="confirmed")
    key = database.delivery_dedup_key("g1", "competition", cid, "contest_time", TARGET)
    assert await reminder.check_general_reminders(AsyncMock(return_value=False), now=NOW) == 0
    await commands.handle_command(f"/修正 {cid} time=2026-11-05 10:00", "g1", "admin")
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW) == 0
    assert database.get_delivery_by_key(key)["status"] == "cancelled"
    await commands.handle_command(f"/修正 {cid} time={TARGET}", "g1", "admin")
    assert database.get_competition(cid)["official_time"] == TARGET
    assert await reminder.check_general_reminders(sender, now=NOW) == 1
    assert await reminder.check_general_reminders(sender, now=NOW) == 0
    sender.assert_awaited_once()
    assert database.get_delivery_by_key(key)["attempts"] == 2


async def test_reconfirm_competition_restores_cancelled_unsent_target(file_db, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1"})
    cid = database.create_competition({"name": "校赛", "official_time": TARGET}, status="confirmed")
    key = database.delivery_dedup_key("g1", "competition", cid, "contest_time", TARGET)
    database.ensure_delivery("g1", "competition", cid, "contest_time", TARGET)
    database.set_competition_status(cid, "draft")
    database.reconcile_deliveries(NOW, allowed_groups={"g1"})
    database.set_competition_status(cid, "confirmed")
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW) == 1
    assert database.get_delivery_by_key(key)["status"] == "sent"
    sender.assert_awaited_once()


def test_cancelled_restore_preserves_exhausted_retry_budget(file_db):
    cid, key = collection()
    token = claim(cid, max_attempts=1)
    database.set_collection_status(cid, "closed")
    database.reconcile_deliveries(NOW + timedelta(seconds=121), max_attempts=1)
    assert database.get_delivery_by_key(key)["status"] == "cancelled"
    database.set_collection_status(cid, "open")
    assert claim(cid, now=NOW + timedelta(seconds=122), max_attempts=1) is None
    assert database.get_delivery_by_key(key)["attempts"] == 1
    assert not database.finish_delivery(key, token, True, now=NOW)


@pytest.mark.parametrize("change", ["closed", "rescheduled", "expired"])
async def test_active_sender_keeps_receipt_right_when_object_becomes_invalid(file_db, change):
    cid, key = collection()
    started, release = asyncio.Event(), asyncio.Event()

    async def sender(*args):
        started.set()
        await release.wait()
        return True

    worker = asyncio.create_task(reminder.check_general_reminders(sender, now=NOW))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        before = database.get_delivery_by_key(key)
        current = NOW + timedelta(seconds=1)
        if change == "closed":
            database.set_collection_status(cid, "closed")
        elif change == "rescheduled":
            with database._connect() as conn:
                conn.execute("UPDATE collections SET deadline = '2026-11-05 10:00' WHERE id = ?", (cid,))
        else:
            current = datetime(2026, 11, 2, tzinfo=NOW.tzinfo)
            # 目标已过期但发送租约仍有效，构造该合法边界。
            with database._connect() as conn:
                conn.execute("UPDATE reminder_deliveries SET lease_until = '2026-11-02 00:01:00' WHERE dedup_key = ?", (key,))
            before = database.get_delivery_by_key(key)
        database.reconcile_deliveries(current, allowed_groups={"g1", "g2"})
        assert database.get_delivery_by_key(key) == before
        assert claim(cid, now=current, allowed_groups={"g1"}) is None
    finally:
        release.set()
    assert await asyncio.wait_for(worker, timeout=2) == 1
    row = database.get_delivery_by_key(key)
    assert row["status"] == "sent" and row["sent_at"]


async def test_receipt_projection_error_keeps_durable_success_and_continues_scan(file_db):
    _, key = collection()
    second_id = database.create_collection("g1", "后续提醒", deadline=TARGET)
    with database._connect() as conn:
        conn.execute("""CREATE TRIGGER reject_sent_projection BEFORE UPDATE OF status ON reminder_deliveries
            WHEN NEW.status = 'sent' AND NEW.dedup_key = '%s'
            BEGIN SELECT RAISE(ABORT, 'receipt storage rejected'); END""" % key)
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW) == 2
    assert sender.await_count == 2
    assert all(row["status"] == "sent" and row["sent_at"] for row in database.list_deliveries("g1"))
    assert database.list_pending_deliveries() == []
    assert database.delivery_status_counts("g1") == {"sent": 2}
    with database._connect() as conn:
        assert conn.execute("SELECT status FROM reminder_deliveries WHERE dedup_key = ?", (key,)).fetchone() == ("sending",)
        assert conn.execute("SELECT COUNT(*) FROM reminder_success_receipts").fetchone() == (2,)
        # 即使旧状态投影留在 pending，持久化成功事实仍优先。
        conn.execute("UPDATE reminder_deliveries SET status = 'pending' WHERE dedup_key = ?", (key,))
    assert database.list_pending_deliveries() == []
    assert await reminder.check_general_reminders(sender, now=NOW + timedelta(seconds=121)) == 0
    assert sender.await_count == 2
    assert database.claim_delivery("g1", "collection", second_id, "deadline", TARGET, now=NOW) is None
    # 新进程重建数据库连接，原 UPDATE trigger 仍在，成功事实不依赖内存。
    restart = subprocess.run([
        sys.executable, "-c",
        "import asyncio,json,sys; from datetime import datetime; from zoneinfo import ZoneInfo; "
        "import database,config,reminder; database.DB_PATH=sys.argv[1]; "
        "config.ALLOWED_GROUPS={'g1'}; database.init_db(); "
        "sender=__import__('unittest.mock',fromlist=['AsyncMock']).AsyncMock(return_value=True); "
        "sent=asyncio.run(reminder.check_general_reminders(sender,now=datetime(2026,11,1,12,3,tzinfo=ZoneInfo('Asia/Shanghai')))); "
        "print(json.dumps({'sent':sent,'calls':sender.await_count,'rows':database.list_deliveries('g1')}))",
        str(file_db),
    ], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10, check=True)
    result = json.loads(restart.stdout)
    assert result["sent"] == result["calls"] == 0
    assert all(row["status"] == "sent" and row["attempts"] == 1 for row in result["rows"])


async def test_failed_receipt_database_error_does_not_skip_healthy_later_item(file_db):
    _, key = collection()
    database.create_collection("g1", "后续提醒", deadline=TARGET)
    with database._connect() as conn:
        conn.execute("""CREATE TRIGGER reject_failed_receipt BEFORE UPDATE OF status ON reminder_deliveries
            WHEN NEW.status = 'pending' AND NEW.dedup_key = '%s'
            BEGIN SELECT RAISE(ABORT, 'failed receipt rejected'); END""" % key)
    sender = AsyncMock(side_effect=[False, True])
    assert await reminder.check_general_reminders(sender, now=NOW) == 1
    assert sender.await_count == 2
    assert database.get_delivery_by_key(key)["status"] == "sending"
    assert database.delivery_status_counts("g1") == {"sending": 1, "sent": 1}


async def test_success_fact_insert_error_preserves_inflight_state_and_continues_scan(file_db, caplog):
    _, key = collection()
    database.create_collection("g1", "后续提醒", deadline=TARGET)
    with database._connect() as conn:
        conn.execute("""CREATE TRIGGER reject_success_fact BEFORE INSERT ON reminder_success_receipts
            WHEN NEW.dedup_key = '%s'
            BEGIN SELECT RAISE(ABORT, 'success fact rejected'); END""" % key)
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW) == 1
    assert sender.await_count == 2
    row = database.get_delivery_by_key(key)
    assert row["status"] == "sending" and row["attempts"] == 1
    assert row["lease_token"] and row["lease_until"] and not row["last_error"]
    assert database.delivery_status_counts("g1") == {"sending": 1, "sent": 1}
    assert "消息已发送但成功事实写入失败" in caplog.text
    with database._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM reminder_success_receipts WHERE dedup_key = ?", (key,)).fetchone() == (0,)


async def test_success_fact_is_saved_before_finish_function_can_fail(file_db, monkeypatch):
    _, key = collection()
    database.create_collection("g1", "后续提醒", deadline=TARGET)
    original = reminder.finish_delivery
    calls = 0

    def first_finish_fails(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("finish unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(reminder, "finish_delivery", first_finish_fails)
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW) == 2
    assert await reminder.check_general_reminders(sender, now=NOW + timedelta(seconds=121)) == 0
    assert sender.await_count == 2
    assert database.get_delivery_by_key(key)["status"] == "sent"


@pytest.mark.parametrize("obj_type", ["collection", "competition"])
@pytest.mark.parametrize("existing_plan", ["none", "queued", "failed_once"])
async def test_group_removal_does_not_spend_attempts_and_return_resumes(file_db, monkeypatch, existing_plan, obj_type):
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1"})
    if obj_type == "collection":
        cid, key = collection()
        kind = "deadline"
    else:
        cid = database.create_competition({"name": "校赛", "official_time": TARGET}, status="confirmed")
        kind = "contest_time"
        key = database.delivery_dedup_key("g1", obj_type, cid, kind, TARGET)
    attempts = int(existing_plan == "failed_once")
    if existing_plan == "queued":
        database.ensure_delivery("g1", obj_type, cid, kind, TARGET)
    elif existing_plan == "failed_once":
        assert await reminder.check_general_reminders(AsyncMock(return_value=False), now=NOW) == 0
    monkeypatch.setattr(config, "ALLOWED_GROUPS", set())
    sender = AsyncMock(return_value=True)
    for minutes in range(4):
        assert await reminder.check_general_reminders(sender, now=NOW + timedelta(minutes=minutes)) == 0
        assert database.claim_delivery("g1", obj_type, cid, kind, TARGET, now=NOW, allowed_groups=set()) is None
    sender.assert_not_awaited()
    row = database.get_delivery_by_key(key)
    if existing_plan != "none":
        assert (row["status"], row["attempts"]) == ("cancelled", attempts)
    else:
        assert row is None
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1"})
    assert await reminder.check_general_reminders(sender, now=NOW + timedelta(minutes=4)) == 1
    sender.assert_awaited_once()
    assert database.get_delivery_by_key(key)["attempts"] == attempts + 1


async def test_registration_example_from_actual_reminder_is_accepted(file_db):
    cid, _ = collection()
    item = reminder.build_general_reminders([database.get_collection(cid)], [], NOW)[0]
    example = re.search(r"（(/报名 \d+)）", item["text"]).group(1)
    result = await commands.handle_command(example, "g1", "member")
    assert "报名成功" in result[0]["text"]
    assert database.summarize_collection(cid)["registered"] == 1
