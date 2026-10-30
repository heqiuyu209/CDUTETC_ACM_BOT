"""提醒状态、跨连接领取、租约恢复及旧去重键迁移的离线回归测试。"""

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from types import SimpleNamespace
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
async def test_main_job_continues_after_legacy_send_failure(file_db, monkeypatch, legacy_result):
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

    async def api_send(**kwargs):
        if "旧比赛" in kwargs["content"]:
            legacy_calls.append(kwargs["group_openid"])
            if legacy_result == "timeout":
                await asyncio.Event().wait()
            if legacy_result == "exception":
                raise RuntimeError("legacy SDK rejected")
            return None
        return {"id": "general-sent-id"}

    client = main.ACMBot()
    monkeypatch.setattr(client, "api", SimpleNamespace(post_group_message=AsyncMock(side_effect=api_send)))
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
