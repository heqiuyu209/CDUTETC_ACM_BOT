"""升级后旧群赛事的投递、去重和隔离。"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

from cdutetc import config, database as db, reminder

NOW = datetime(2026, 11, 11, 18, tzinfo=ZoneInfo("Asia/Shanghai"))
TARGET = "2026-11-11 19:00"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "legacy.sqlite"))
    monkeypatch.setattr(config, "BOT_PROTOCOL", "onebot")
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "REMIND_BEFORE_HOURS", 24)
    monkeypatch.setattr(config, "REMIND_OFFSET_HOURS", (24, 1))
    monkeypatch.setattr(config, "CONTEST_REMIND_OFFSETS_MINUTES", (30, 15))
    monkeypatch.setattr(db, "_delivery_now", lambda value=None: value or NOW)
    db.init_db()


def legacy(group="g1", name="旧比赛"):
    cid = db.save_contest(group, {"name": name, "reg_deadline": TARGET})
    key = db.delivery_dedup_key(group, "legacy_contest", cid, "reg_deadline", TARGET)
    return cid, key


async def test_legacy_delivery_concurrent_and_restart(env):
    _, key = legacy()
    entered, release = asyncio.Event(), asyncio.Event()
    sender = Mock()

    async def send(group, text):
        sender(group, text)
        entered.set()
        await release.wait()
        return True

    first = asyncio.create_task(reminder.check_general_reminders(send, now=NOW))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert await reminder.check_general_reminders(send, now=NOW) == 0
    finally:
        release.set()
    assert await first == 1
    db.init_db()
    assert await reminder.check_general_reminders(send, now=NOW) == 0
    assert sender.call_count == 1
    assert db.get_delivery_by_key(key)["status"] == "sent"


async def test_old_success_and_scope_are_preserved(env):
    cid, _ = legacy()
    db.mark_reminded("g1", cid, "reg_deadline", TARGET)
    legacy("", "未归属")
    legacy("unauthorized", "未授权")
    legacy("g2", "本群比赛")
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW) == 1
    sender.assert_awaited_once()
    assert sender.await_args.args[0] == "g2"
    assert "本群比赛" in sender.await_args.args[1]


async def test_legacy_failures_retry_with_limit(env):
    _, key = legacy()
    sender = AsyncMock(return_value=False)
    for _ in range(3):
        assert await reminder.check_general_reminders(sender, now=NOW, max_attempts=2) == 0
    assert sender.await_count == 2
    assert (db.get_delivery_by_key(key)["status"], db.get_delivery_by_key(key)["attempts"]) == ("failed", 2)


async def test_legacy_change_before_send_invalidates_claim(env, monkeypatch):
    cid, key = legacy()
    claim = reminder.claim_delivery

    def claim_then_change(*args, **kwargs):
        token = claim(*args, **kwargs)
        with db._connect() as conn:
            conn.execute("UPDATE contests SET reg_deadline='2026-11-11 20:00' WHERE id=?", (cid,))
        return token

    monkeypatch.setattr(reminder, "claim_delivery", claim_then_change)
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW) == 0
    sender.assert_not_awaited()
    assert db.get_delivery_by_key(key)["status"] == "cancelled"


async def test_legacy_reschedule_creates_new_identity(env):
    cid, _ = legacy()
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW) == 1
    with db._connect() as conn:
        conn.execute("UPDATE contests SET reg_deadline='2026-11-11 20:00' WHERE id=?", (cid,))
    assert await reminder.check_general_reminders(sender, now=NOW) == 1
    assert await reminder.check_general_reminders(sender, now=NOW) == 0
    assert sender.await_count == 2


async def test_legacy_success_survives_old_projection_failure(env):
    cid, key = legacy()
    with db._connect() as conn:
        conn.execute("CREATE TRIGGER reject_legacy_mark BEFORE INSERT ON reminders "
                     "BEGIN SELECT RAISE(ABORT, 'legacy mark rejected'); END")
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW) == 1
    assert db.has_reminded("g1", cid, "reg_deadline", TARGET)
    assert db.get_delivery_by_key(key)["status"] == "sent"
    db.init_db()
    assert await reminder.check_general_reminders(sender, now=NOW) == 0
    sender.assert_awaited_once()


async def test_legacy_group_pause_does_not_consume_attempt(env, monkeypatch):
    monkeypatch.setattr(config, "BOT_PROTOCOL", "qqbot")
    _, key = legacy()
    db.block_group_notifications("qqbot", "g1", "active_forbidden", 40034105)
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW) == 0
    assert db.get_delivery_by_key(key) is None
    sender.assert_not_awaited()


@pytest.mark.parametrize("field", ["reg_deadline", "contest_time"])
async def test_legacy_respects_group_switch_and_resumes(env, field):
    cid = db.save_contest("g1", {"name": "暂停群比赛", field: TARGET})
    db.save_contest("g2", {"name": "开启群比赛", field: TARGET})
    key = db.delivery_dedup_key("g1", "legacy_contest", cid, field, TARGET)
    db.set_group_switch("g1", "contest_remind_enabled", False, "admin")
    sender = AsyncMock(return_value=True)

    assert await reminder.check_general_reminders(sender, now=NOW) == 1
    sender.assert_awaited_once()
    assert sender.await_args.args[0] == "g2"
    assert db.get_delivery_by_key(key) is None

    db.init_db()
    assert await reminder.check_general_reminders(sender, now=NOW) == 0
    db.set_group_switch("g1", "contest_remind_enabled", True, "admin")
    assert await reminder.check_general_reminders(sender, now=NOW) == 1
    assert sender.await_args.args[0] == "g1"
    assert db.get_delivery_by_key(key)["attempts"] == 1
    assert await reminder.check_general_reminders(sender, now=NOW) == 0


async def test_confirmed_migration_does_not_send_legacy_copy(env):
    cid, key = legacy()
    migrated = db.migrate_contest_to_competition(cid, "g1")
    db.confirm_competition(migrated)
    sender = AsyncMock(return_value=True)
    await reminder.check_general_reminders(sender, now=NOW)
    assert db.get_delivery_by_key(key) is None or db.get_delivery_by_key(key)["status"] == "cancelled"
    assert {call.args[0] for call in sender.await_args_list} == {"g1", "g2"}
    assert sender.await_count == 2
    assert all(call.args[1].startswith("【报名提醒】") for call in sender.await_args_list)


@pytest.mark.parametrize("field", ["reg_deadline", "contest_time"])
async def test_confirmed_migration_returned_to_draft_keeps_legacy_retired(env, monkeypatch, field):
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1"})
    cid = db.save_contest("g1", {"name": "待重新审核的比赛", field: TARGET})
    key = db.delivery_dedup_key("g1", "legacy_contest", cid, field, TARGET)
    migrated = db.migrate_contest_to_competition(cid, "g1")
    db.confirm_competition(migrated)
    db.set_competition_status(migrated, "draft")
    sender = AsyncMock(return_value=True)

    assert await reminder.check_general_reminders(sender, now=NOW) == 0
    sender.assert_not_awaited()
    db.init_db()
    assert await reminder.check_general_reminders(sender, now=NOW) == 0
    assert db.get_delivery_by_key(key)["status"] == "cancelled"

    db.confirm_competition(migrated)
    if field == "reg_deadline":
        assert await reminder.check_general_reminders(sender, now=NOW) == 1
    else:
        assert await reminder.check_contest_start_reminders(sender, now=NOW + timedelta(minutes=30)) == 1
    sender.assert_awaited_once()


def test_confirmed_migration_returned_to_draft_invalidates_unsent_claim(env):
    cid, key = legacy()
    token = db.claim_delivery("g1", "legacy_contest", cid, "reg_deadline", TARGET,
                              now=NOW, allowed_groups={"g1"})
    assert token
    migrated = db.migrate_contest_to_competition(cid, "g1")
    db.confirm_competition(migrated)
    db.set_competition_status(migrated, "draft")

    assert not db.validate_delivery_claim(key, token, now=NOW, allowed_groups={"g1"})
    assert db.get_delivery_by_key(key)["status"] == "cancelled"


async def test_unconfirmed_migration_preserves_existing_legacy_reminder(env):
    cid, key = legacy()
    migrated = db.migrate_contest_to_competition(cid, "g1")
    assert db.get_competition(migrated)["status"] == "draft"
    sender = AsyncMock(return_value=True)

    assert await reminder.check_general_reminders(sender, now=NOW) == 1
    sender.assert_awaited_once()
    assert sender.await_args.args[0] == "g1"
    assert db.get_delivery_by_key(key)["status"] == "sent"


@pytest.mark.parametrize("field", ["reg_deadline", "contest_time"])
async def test_migrated_competition_respects_legacy_success(env, monkeypatch, field):
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1"})
    cid = db.save_contest("g1", {"name": "旧比赛", field: TARGET})
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW) == 1
    migrated = db.migrate_contest_to_competition(cid, "g1")
    db.confirm_competition(migrated)
    if field == "reg_deadline":
        assert await reminder.check_general_reminders(sender, now=NOW) == 0
    else:
        assert await reminder.check_contest_start_reminders(sender, now=NOW + timedelta(minutes=30)) == 0
    assert sender.await_count == 1


@pytest.mark.parametrize("old_send_succeeded", [False, True])
async def test_migration_waits_for_inflight_legacy_delivery(env, monkeypatch, old_send_succeeded):
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1"})
    cid, key = legacy()
    token = db.claim_delivery("g1", "legacy_contest", cid, "reg_deadline", TARGET,
                              now=NOW, allowed_groups={"g1"})
    assert token
    migrated = db.migrate_contest_to_competition(cid, "g1")
    db.confirm_competition(migrated)
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW) == 0
    sender.assert_not_awaited()
    assert db.finish_delivery(key, token, old_send_succeeded, now=NOW)
    assert await reminder.check_general_reminders(sender, now=NOW) == (0 if old_send_succeeded else 1)
    assert sender.await_count == (0 if old_send_succeeded else 1)
