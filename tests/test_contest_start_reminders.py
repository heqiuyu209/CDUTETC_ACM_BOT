"""Transactional contest-start reminder lifecycle tests."""
import asyncio
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock

import pytest

from cdutetc import config, database, reminder

TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def db_conn(monkeypatch):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "WEEKLY_CONTEST_GROUPS", set())
    monkeypatch.setattr(config, "CONTEST_REMIND_OFFSETS_MINUTES", (30, 15))
    monkeypatch.setattr(config, "CONTEST_REMIND_GRACE_SECONDS", 120)
    monkeypatch.setattr(config, "PUBLISH_SCOPE_TO_SOURCE", True)
    monkeypatch.setattr(database, "_delivery_now", lambda value=None: value or datetime(2026, 11, 6, 10, 0, tzinfo=TZ))
    database.init_db()
    yield conn
    conn.close()


def _time(dt):
    return dt.strftime("%Y-%m-%d %H:%M")


def _future(minutes=90):
    value = datetime.now(TZ).replace(second=0, microsecond=0) + timedelta(minutes=minutes)
    return value


def _pending(group="g1", contest_time=None):
    return database.create_pending_publication(
        {"name": "事务比赛", "official_time": _time(contest_time or _future())},
        group, "", "", "teacher",
    )


def _plans(conn, cid):
    return conn.execute(
        "SELECT group_id, target_time, status, scheduled_at, expires_at, error_code, dedup_key"
        " FROM reminder_deliveries WHERE obj_type='competition' AND obj_id=?"
        " AND remind_type='contest_start' ORDER BY group_id, scheduled_at", (cid,)
    ).fetchall()


def test_approval_transaction_persists_only_source_group_and_idempotent(db_conn):
    pid = _pending()
    cid, created = database.approve_pending_publication(pid, "admin")
    assert created is True
    assert database.get_pending_publication(pid)["status"] == "approved"
    plans = [p for p in _plans(db_conn, cid) if p[2] == "pending"]
    assert [p[0] for p in plans] == ["g1", "g1"]
    assert [p[2] for p in plans] == ["pending", "pending"]
    assert len({p[6] for p in plans}) == 2
    assert all(p[3] and p[4] for p in plans)
    target = datetime.strptime(plans[0][1], "%Y-%m-%d %H:%M")
    assert [p[3] for p in plans] == [
        (target - timedelta(minutes=offset)).strftime("%Y-%m-%d %H:%M:%S")
        for offset in (30, 15)
    ]
    assert [p[4] for p in plans] == [
        (target - timedelta(minutes=offset) + timedelta(seconds=120)).strftime("%Y-%m-%d %H:%M:%S")
        for offset in (30, 15)
    ]
    assert database.get_competition_notify_groups(cid) == {"g1"}

    same_id, created_again = database.approve_pending_publication(pid, "admin")
    assert (same_id, created_again) == (cid, False)
    assert len([p for p in _plans(db_conn, cid) if p[2] == "pending"]) == 2


def test_approval_failure_rolls_back_competition_scope_and_plans(db_conn):
    pid = _pending()
    db_conn.execute(
        "CREATE TRIGGER fail_approval BEFORE UPDATE OF status ON pending_publications"
        " WHEN NEW.status='approved' BEGIN SELECT RAISE(ABORT, 'forced failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        database.approve_pending_publication(pid, "admin")
    assert database.get_pending_publication(pid)["status"] == "pending"
    assert db_conn.execute("SELECT COUNT(*) FROM competitions").fetchone()[0] == 0
    assert db_conn.execute("SELECT COUNT(*) FROM reminder_deliveries").fetchone()[0] == 0


def test_amend_time_cancels_old_nodes_and_creates_new_future_only(db_conn, monkeypatch):
    now = datetime(2026, 11, 6, 10, 0, tzinfo=TZ)
    monkeypatch.setattr(database, "_delivery_now", lambda value=None: now)
    pid = _pending(contest_time=datetime(2026, 11, 6, 12, 0, tzinfo=TZ))
    cid, _ = database.approve_pending_publication(pid, "admin")
    old = [p for p in _plans(db_conn, cid) if p[2] == "pending"]
    assert len(old) == 2
    assert database.amend_competition(
        cid, {"official_time": "2026-11-06 13:00"}, operator="admin"
    )
    rows = db_conn.execute(
        "SELECT group_id, target_time, status, scheduled_at FROM reminder_deliveries"
        " WHERE obj_id=? AND remind_type='contest_start' ORDER BY id", (cid,)
    ).fetchall()
    old_rows = [r for r in rows if r[1] == "2026-11-06 12:00"]
    assert len(old_rows) == 2
    assert all(r[2] == "cancelled" for r in old_rows)
    active = [r for r in rows if r[2] == "pending"]
    assert len(active) == 2
    assert {r[0] for r in active} == {"g1"}
    assert {r[1] for r in active} == {"2026-11-06 13:00"}


def test_approval_skips_elapsed_nodes_and_never_backfills_after_restart(db_conn, monkeypatch):
    now = datetime(2026, 11, 6, 10, 0, tzinfo=TZ)
    monkeypatch.setattr(database, "_delivery_now", lambda value=None: now)
    pid = _pending(contest_time=datetime(2026, 11, 6, 10, 20, tzinfo=TZ))
    cid, _ = database.approve_pending_publication(pid, "admin")
    remaining = [p for p in _plans(db_conn, cid) if p[2] == "pending"]
    assert len(remaining) == 1
    assert remaining[0][3:5] == ("2026-11-06 10:05:00", "2026-11-06 10:07:00")
    assert remaining[0][6].endswith("|0.25")

    pid_later = _pending(contest_time=datetime(2026, 11, 6, 11, 0, tzinfo=TZ))
    cid_later, _ = database.approve_pending_publication(pid_later, "admin")
    assert len([p for p in _plans(db_conn, cid_later) if p[2] == "pending"]) == 2
    after_restart = now + timedelta(minutes=48)
    monkeypatch.setattr(database, "_delivery_now", lambda value=None: value or after_restart)
    sent = AsyncMock(return_value=True)
    assert asyncio.run(reminder.check_contest_start_reminders(sent, now=after_restart)) == 0
    sent.assert_not_awaited()
    statuses = {row[0] for row in db_conn.execute(
        "SELECT DISTINCT status FROM reminder_deliveries WHERE obj_id=?", (cid_later,)
    ).fetchall()}
    assert statuses <= {"expired", "cancelled"}
    assert "expired" in statuses


async def test_due_node_retries_within_grace_and_records_structured_error(db_conn, monkeypatch):
    now = datetime(2026, 11, 6, 10, 14, tzinfo=TZ)
    monkeypatch.setattr(database, "_delivery_now", lambda value=None: value or now)
    pid = _pending(contest_time=datetime(2026, 11, 6, 10, 30, tzinfo=TZ))
    cid, _ = database.approve_pending_publication(pid, "admin")
    # 15m node is due at 10:15; inject stable adapter result shape.
    class DeliveryResult:
        ok = False
        code = "group_forbidden"
        user_hint = "群聊发送权限不足"
    send = AsyncMock(return_value=DeliveryResult())
    assert await reminder.check_contest_start_reminders(send, now=now) == 0
    send.assert_not_awaited()
    assert db_conn.execute(
        "SELECT attempts FROM reminder_deliveries WHERE obj_id=?", (cid,)
    ).fetchone() == (0,)
    due = datetime(2026, 11, 6, 10, 15, tzinfo=TZ)
    assert await reminder.check_contest_start_reminders(send, now=due) == 0
    send.assert_awaited_once()
    failed = db_conn.execute(
        "SELECT status, attempts, error_code FROM reminder_deliveries"
        " WHERE obj_id=? AND scheduled_at='2026-11-06 10:15:00'", (cid,)
    ).fetchone()
    assert failed == ("pending", 1, "group_forbidden")

    later = datetime(2026, 11, 6, 10, 15, 30, tzinfo=TZ)
    send = AsyncMock(return_value=True)
    assert await reminder.check_contest_start_reminders(send, now=later) == 1
    assert send.await_args.args[0] == "g1"
    assert db_conn.execute(
        "SELECT status, attempts, error_code FROM reminder_deliveries WHERE obj_id=?"
        " AND scheduled_at='2026-11-06 10:15:00'", (cid,)
    ).fetchone() == ("sent", 2, "")


def test_approval_node_insert_failure_rolls_back_all_business_writes(db_conn):
    pid = _pending()
    db_conn.execute(
        "CREATE TRIGGER fail_plan BEFORE INSERT ON reminder_deliveries"
        " WHEN NEW.remind_type='contest_start' BEGIN SELECT RAISE(ABORT, 'plan write failed'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        database.approve_pending_publication(pid, "admin")
    assert database.get_pending_publication(pid)["status"] == "pending"
    for table in ("competitions", "competition_notify_groups", "reminder_deliveries"):
        assert db_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


async def test_scope_change_only_sends_current_group_and_restart_is_idempotent(db_conn, monkeypatch):
    now = datetime(2026, 11, 6, 10, 0, tzinfo=TZ)
    monkeypatch.setattr(database, "_delivery_now", lambda value=None: value or now)
    pid = _pending(contest_time=now + timedelta(hours=1))
    cid, _ = database.approve_pending_publication(pid, "admin")
    database.set_competition_notify_groups(cid, {"g2"}, operator="admin")
    plans = _plans(db_conn, cid)
    assert len(plans) == 4
    assert {p[0] for p in plans if p[2] == "pending"} == {"g2"}
    assert len([p for p in plans if p[0] == "g1" and p[2] == "cancelled"]) == 2
    sender = AsyncMock(return_value=True)
    due = now + timedelta(minutes=30)
    assert await reminder.check_contest_start_reminders(sender, now=due) == 1
    assert sender.await_args.args[0] == "g2"
    assert "30分钟" in sender.await_args.args[1]
    database.init_db()  # startup migration preserves both plan identities and success receipt
    assert await reminder.check_contest_start_reminders(sender, now=due) == 0
    assert await reminder.check_contest_start_reminders(sender, now=now + timedelta(minutes=45)) == 1
    assert sender.await_count == 2
    assert all(call.args[0] == "g2" for call in sender.await_args_list)
    assert "15分钟" in sender.await_args.args[1]
    assert len([p for p in _plans(db_conn, cid) if p[2] == "sent"]) == 2


def test_weekly_confirmed_creation_uses_weekly_group_scope(db_conn, monkeypatch):
    monkeypatch.setattr(config, "WEEKLY_CONTEST_GROUPS", {"g2"})
    now = _future(90)
    week = now.date() - timedelta(days=now.weekday())
    # Use a future contest time compatible with the current test clock.
    contest_time = _time(now + timedelta(minutes=60))
    cid, created = database.create_weekly_competition(
        week.isoformat(), {"name": "周赛事务测试", "official_time": contest_time}
    )
    assert created
    assert {row[0] for row in _plans(db_conn, cid) if row[2] == "pending"} == {"g2"}
