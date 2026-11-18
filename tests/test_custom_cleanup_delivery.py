"""自定义提醒领取、回执和状态推进与在线群清理的并发回归。"""

import asyncio
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from cdutetc import config, database as db, group_cleanup, reminder

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=TZ)
TARGET = NOW.strftime("%Y-%m-%d %H:%M")


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "custom_cleanup.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    monkeypatch.setattr(db, "_delivery_now", lambda value=None: value or NOW)
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    db.init_db()
    return path


def _rows(path, sql, args=()):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(sql, args).fetchall()


def _seed(*, repeat="none", group="g1", target=TARGET):
    return db.create_custom_reminder(group, "交周报", target,
                                     repeat_rule=repeat, weekday="三" if repeat == "weekly" else "",
                                     time_of_day="12:00", operator="admin")


def _preview():
    return group_cleanup.preview_cleanup("g1", "test_data", days=0, operator="admin", now=NOW)


def _apply(preview):
    return group_cleanup.apply_cleanup("g1", preview["token"], operator="admin", now=NOW)


def _deliveries(path, rid):
    return _rows(path, "SELECT status,attempts,lease_token,lease_until,target_time"
                 " FROM reminder_deliveries WHERE obj_type='custom_reminder' AND obj_id=? ORDER BY id", (rid,))


async def test_due_one_shot_has_one_lease_one_receipt_and_no_repeat(env):
    rid = _seed()
    send = AsyncMock(return_value=True)
    assert await reminder.check_custom_reminders(send, now=NOW) == 1
    assert await reminder.check_custom_reminders(send, now=NOW) == 0
    send.assert_awaited_once_with("g1", "【提醒】交周报")
    assert db.get_custom_reminder(rid)["status"] == "done"
    assert _deliveries(env, rid) == [("sent", 1, "", "", TARGET)]
    receipts = _rows(env, "SELECT dedup_key,lease_token FROM reminder_success_receipts")
    assert len(receipts) == 1 and receipts[0][1]
    assert receipts[0][0] == db.delivery_dedup_key("g1", "custom_reminder", rid, "due", TARGET)


@pytest.mark.parametrize("repeat", ["none", "weekly"])
async def test_scan_recovers_committed_receipt_after_projection_failure_without_resending(env, repeat):
    rid = _seed(repeat=repeat)
    with closing(sqlite3.connect(env)) as conn, conn:
        conn.execute("CREATE TRIGGER fail_projection BEFORE UPDATE ON custom_reminders"
                     " BEGIN SELECT RAISE(ABORT,'projection failed'); END")
    send = AsyncMock(return_value=True)
    assert await reminder.check_custom_reminders(send, now=NOW) == 0
    assert db.get_custom_reminder(rid)["status"] == "pending"
    assert _rows(env, "SELECT 1 FROM reminder_success_receipts")
    with closing(sqlite3.connect(env)) as conn, conn:
        conn.execute("DROP TRIGGER fail_projection")
    # 模拟重启时回执投影已恢复为 sent，但自定义提醒尚未推进。
    db.init_db()
    assert await reminder.check_custom_reminders(send, now=NOW) == 0
    assert send.await_count == 1
    fresh = db.get_custom_reminder(rid)
    if repeat == "weekly":
        assert fresh["status"] == "pending"
        assert fresh["target_time"] == (NOW + timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
    else:
        assert fresh["status"] == "done"
    assert _deliveries(env, rid) == [("sent", 1, "", "", TARGET)]
    receipts = _rows(env, "SELECT dedup_key,lease_token FROM reminder_success_receipts")
    assert len(receipts) == 1 and receipts[0][1]
    assert receipts[0][0] == db.delivery_dedup_key("g1", "custom_reminder", rid, "due", TARGET)


async def test_failed_send_stops_at_retry_limit_without_completing_reminder(env):
    rid = _seed()
    send = AsyncMock(return_value=False)
    for _ in range(5):
        assert await reminder.check_custom_reminders(send, now=NOW, max_attempts=2) == 0
    assert send.await_count == 2
    assert db.get_custom_reminder(rid)["status"] == "pending"
    assert _deliveries(env, rid) == [("failed", 2, "", "", TARGET)]
    assert not _rows(env, "SELECT 1 FROM reminder_success_receipts")


async def test_weekly_success_advances_once_and_next_occurrence_gets_own_delivery(env):
    rid = _seed(repeat="weekly")
    send = AsyncMock(return_value=True)
    assert await reminder.check_custom_reminders(send, now=NOW) == 1
    next_week = NOW + timedelta(days=7)
    row = db.get_custom_reminder(rid)
    assert row["status"] == "pending" and row["repeat_rule"] == "weekly"
    assert row["target_time"] == next_week.strftime("%Y-%m-%d %H:%M")
    assert await reminder.check_custom_reminders(send, now=NOW) == 0
    assert await reminder.check_custom_reminders(send, now=next_week) == 1
    assert send.await_count == 2
    assert db.get_custom_reminder(rid)["target_time"] == (NOW + timedelta(days=14)).strftime("%Y-%m-%d %H:%M")
    assert [row[0] for row in _deliveries(env, rid)] == ["sent", "sent"]
    assert len(_rows(env, "SELECT 1 FROM reminder_success_receipts")) == 2


async def test_parallel_scans_share_claim_and_only_one_sends(env):
    rid = _seed()
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked_send(group_id, text):
        entered.set()
        await release.wait()
        return True
    send = AsyncMock(side_effect=blocked_send)
    first = asyncio.create_task(reminder.check_custom_reminders(send, now=NOW))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert await reminder.check_custom_reminders(send, now=NOW) == 0
        assert send.await_count == 1
        active = _deliveries(env, rid)
        assert len(active) == 1 and active[0][0] == "sending" and active[0][2]
    finally:
        release.set()
        result = await first
    assert result == 1
    assert _deliveries(env, rid)[0][0] == "sent"


async def test_group_cleanup_waits_for_active_send_then_fresh_preview_can_delete(env):
    rid = _seed()
    other = _seed(group="g2", target=(NOW + timedelta(days=1)).strftime("%Y-%m-%d %H:%M"))
    group_cleanup.mark_test("g1", "custom_reminder", rid, operator="admin", now=NOW)
    before_send = _preview()
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked_send(group_id, text):
        entered.set()
        await release.wait()
        return True
    sending = asyncio.create_task(reminder.check_custom_reminders(blocked_send, now=NOW))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        active = _deliveries(env, rid)
        assert active[0][0] == "sending" and active[0][2]
        result = _apply(before_send)
        assert not result["cleared"].get("custom_reminder", 0) and result["skipped"] >= 1
        assert _deliveries(env, rid) == active
        assert db.get_custom_reminder(rid) is not None
        assert not _preview()["items"]
    finally:
        release.set()
        sent = await sending
    assert sent == 1 and db.get_custom_reminder(rid)["status"] == "done"
    result = _apply(_preview())
    assert result["cleared"].get("custom_reminder") == 1
    assert db.get_custom_reminder(rid) is None and db.get_custom_reminder(other) is not None
    assert _deliveries(env, rid)[0][0] == "sent"
    assert len(_rows(env, "SELECT 1 FROM reminder_success_receipts")) == 1


async def test_cached_due_snapshot_cannot_send_after_cleanup(env, monkeypatch):
    rid = _seed()
    stale = db.list_due_custom_reminders(NOW)
    group_cleanup.mark_test("g1", "custom_reminder", rid, operator="admin", now=NOW)
    assert _apply(_preview())["cleared"].get("custom_reminder") == 1
    monkeypatch.setattr(db, "list_due_custom_reminders", lambda *_args, **_kwargs: stale)
    send = AsyncMock(return_value=True)
    assert await reminder.check_custom_reminders(send, now=NOW) == 0
    send.assert_not_awaited()
    assert not _rows(env, "SELECT 1 FROM reminder_success_receipts")


@pytest.mark.parametrize("repeat", ["none", "weekly"])
async def test_completed_old_send_does_not_overwrite_admin_new_time_or_content(env, repeat):
    rid = _seed(repeat=repeat)
    new_target = (NOW + timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
    async def modify_while_sending(group_id, text):
        assert _deliveries(env, rid)[0][0] == "sending"
        assert db.update_custom_reminder(rid, target_time=new_target, content="更新后的正式安排", operator="admin")
        return True
    assert await reminder.check_custom_reminders(modify_while_sending, now=NOW) == 1
    row = db.get_custom_reminder(rid)
    assert row["target_time"] == new_target and row["content"] == "更新后的正式安排"
    assert row["status"] == "pending" and row["repeat_rule"] == repeat
    assert _deliveries(env, rid) == [("sent", 1, "", "", TARGET)]
    send = AsyncMock(return_value=True)
    assert await reminder.check_custom_reminders(send, now=NOW) == 0
    send.assert_not_awaited()


def test_completion_requires_actual_receipt_and_matching_claim(env):
    rid = _seed()
    snapshot = db.get_custom_reminder(rid)
    key = db.delivery_dedup_key("g1", "custom_reminder", rid, "due", TARGET)
    token = db.claim_delivery("g1", "custom_reminder", rid, "due", TARGET, now=NOW,
                              allowed_groups={"g1", "g2"})
    assert token
    assert not db.complete_custom_reminder(snapshot, key, token, now=NOW)
    assert db.get_custom_reminder(rid)["status"] == "pending"
    assert _deliveries(env, rid)[0][0] == "sending"
    assert db.record_delivery_success(key, token, now=NOW)
    assert not db.complete_custom_reminder(snapshot, key, "not-owner", now=NOW)
    assert db.get_custom_reminder(rid)["status"] == "pending"
    assert db.complete_custom_reminder(snapshot, key, token, now=NOW)
    assert db.get_custom_reminder(rid)["status"] == "done"
    assert _deliveries(env, rid)[0][0] == "sent"


@pytest.mark.parametrize("repeat", ["none", "weekly"])
async def test_content_only_edit_during_send_preserves_content_and_completes_occurrence(env, repeat):
    rid = _seed(repeat=repeat)
    async def edit_content(group_id, text):
        assert db.update_custom_reminder(rid, content="新内容", operator="admin")
        return True
    assert await reminder.check_custom_reminders(edit_content, now=NOW) == 1
    fresh = db.get_custom_reminder(rid)
    assert fresh["content"] == "新内容"
    if repeat == "none":
        assert fresh["status"] == "done"
    else:
        assert fresh["target_time"] == (NOW + timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
        send = AsyncMock(return_value=True)
        assert await reminder.check_custom_reminders(send, now=NOW + timedelta(days=7)) == 1
        send.assert_awaited_once_with("g1", "【提醒】新内容")


async def test_failed_receipt_recovery_does_not_block_another_group(env):
    rid = _seed()
    with closing(sqlite3.connect(env)) as conn, conn:
        conn.execute(f"CREATE TRIGGER fail_projection BEFORE UPDATE ON custom_reminders WHEN OLD.id={rid}"
                     " BEGIN SELECT RAISE(ABORT,'projection failed'); END")
    send = AsyncMock(return_value=True)
    assert await reminder.check_custom_reminders(send, now=NOW) == 0
    healthy = _seed(group="g2")
    assert await reminder.check_custom_reminders(send, now=NOW) == 1
    assert db.get_custom_reminder(healthy)["status"] == "done"
    assert send.await_count == 2


def test_completion_rolls_back_projection_and_retains_success_receipt_on_sql_failure(env):
    rid = _seed()
    snapshot = db.get_custom_reminder(rid)
    key = db.delivery_dedup_key("g1", "custom_reminder", rid, "due", TARGET)
    token = db.claim_delivery("g1", "custom_reminder", rid, "due", TARGET, now=NOW,
                              allowed_groups={"g1", "g2"})
    assert db.record_delivery_success(key, token, now=NOW)
    with closing(sqlite3.connect(env)) as conn, conn:
        conn.execute("CREATE TRIGGER fail_delivery_complete BEFORE UPDATE ON reminder_deliveries"
                     " WHEN NEW.status='sent' BEGIN SELECT RAISE(ABORT,'completion failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match="completion failed"):
        db.complete_custom_reminder(snapshot, key, token, now=NOW)
    assert db.get_custom_reminder(rid)["status"] == "pending"
    assert _deliveries(env, rid)[0][0] == "sending"
    assert _rows(env, "SELECT 1 FROM reminder_success_receipts WHERE dedup_key=?", (key,))
    with closing(sqlite3.connect(env)) as conn, conn:
        conn.execute("DROP TRIGGER fail_delivery_complete")
    assert db.complete_custom_reminder(snapshot, key, token, now=NOW)
    assert db.get_custom_reminder(rid)["status"] == "done"
