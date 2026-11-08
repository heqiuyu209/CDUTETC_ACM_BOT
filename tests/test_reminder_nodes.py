"""多节点在跨重启、旧成功历史和主入口中的可观察投递行为。"""

from datetime import datetime, timedelta
import sqlite3
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from cdutetc import config, database, llm, main, reminder
from cdutetc.errors import DeliveryCode, DeliveryResult

TZ = ZoneInfo("Asia/Shanghai")
TARGET = datetime(2026, 11, 8, 19, tzinfo=TZ)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "nodes.sqlite"))
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1"})
    monkeypatch.setattr(config, "REMIND_OFFSET_HOURS", (24, 1))
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    monkeypatch.setattr(database, "_delivery_now", lambda value=None: value or TARGET - timedelta(days=2))
    monkeypatch.setattr(llm, "_chat_once", AsyncMock(side_effect=AssertionError("提醒不应调用模型")))
    database.init_db()


@pytest.mark.parametrize("kind", ["collection", "competition", "training_session"])
async def test_both_nodes_send_once_across_restart_without_llm(db, kind):
    target = TARGET.strftime("%Y-%m-%d %H:%M")
    if kind == "collection":
        database.create_collection("g1", "报名", deadline=target)
    elif kind == "competition":
        database.create_competition({"name": "校赛", "official_reg_deadline": target}, status="confirmed")
    else:
        database.create_training_session("g1", {"title": "训练", "weekday": "周日", "start_time": "19:00"})
    sender = AsyncMock(return_value=True)
    for hours in (24, 1):
        now = TARGET - timedelta(hours=hours)
        assert await reminder.check_general_reminders(sender, now=now) == 1
        database.init_db()
        assert await reminder.check_general_reminders(sender, now=now) == 0
    assert sender.await_count == 2
    rows = database.list_deliveries("g1")
    assert {row["dedup_key"].rsplit("|", 1)[1] for row in rows} == {"24", "1"}
    assert all(row["status"] == "sent" and row["attempts"] == 1 for row in rows)
    llm._chat_once.assert_not_awaited()


async def test_old_single_node_success_is_not_replayed_after_upgrade(db):
    target = TARGET.strftime("%Y-%m-%d %H:%M")
    cid = database.create_collection("g1", "旧报名", deadline=target)
    key = database.delivery_dedup_key("g1", "collection", cid, "deadline", target)
    now = TARGET - timedelta(hours=24)
    token = database.claim_delivery("g1", "collection", cid, "deadline", target, now=now)
    assert database.finish_delivery(key, token, True, now=now)
    database.init_db()
    sender = AsyncMock(return_value=True)
    for hours in (24, 1):
        assert await reminder.check_general_reminders(sender, now=TARGET - timedelta(hours=hours)) == 0
    sender.assert_not_awaited()


async def test_main_general_reminder_keeps_platform_error_code(db, monkeypatch):
    database.create_collection("g1", "报名", deadline=TARGET.strftime("%Y-%m-%d %H:%M"))
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return TARGET - timedelta(hours=1)
    monkeypatch.setattr(reminder, "datetime", Clock)
    monkeypatch.setattr(main, "check_reminders", AsyncMock(return_value=0))
    bot = main.ACMBot()
    monkeypatch.setattr(bot, "_send_group_result", AsyncMock(return_value=DeliveryResult(
        False, DeliveryCode.FORBIDDEN, "onebot",
    )))
    await bot._remind_job()
    row = database.list_deliveries("g1")[0]
    assert (row["status"], row["attempts"], row["error_code"]) == ("pending", 1, "forbidden")


async def test_short_start_node_expires_at_contest_start(db, monkeypatch):
    monkeypatch.setattr(config, "CONTEST_REMIND_OFFSETS_MINUTES", (1,))
    database.create_competition({"name": "短节点比赛", "official_time": TARGET.strftime("%Y-%m-%d %H:%M")}, status="confirmed")
    sender = AsyncMock(return_value=True)
    assert await reminder.check_contest_start_reminders(sender, now=TARGET) == 0
    sender.assert_not_awaited()
    assert database.list_deliveries("g1")[0]["status"] == "expired"


async def test_delayed_start_message_uses_actual_countdown(db):
    database.create_competition({"name": "比赛", "official_time": TARGET.strftime("%Y-%m-%d %H:%M")}, status="confirmed")
    sender = AsyncMock(return_value=True)
    assert await reminder.check_contest_start_reminders(sender, now=TARGET - timedelta(minutes=29)) == 1
    assert "约29分钟" in sender.await_args.args[1]
    assert "30分钟" not in sender.await_args.args[1]


async def test_unreadable_scope_never_broadcasts_and_other_objects_continue(db, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2"})
    target = TARGET.strftime("%Y-%m-%d %H:%M")
    cid = database.create_competition({"name": "仅限g1的比赛", "official_reg_deadline": target}, status="confirmed")
    database.set_competition_notify_groups(cid, {"g1"})
    database.create_collection("g2", "另一群正常报名", deadline=target)
    def unavailable(*args):
        raise sqlite3.OperationalError("scope unavailable")
    monkeypatch.setattr(reminder, "get_competition_target_groups", unavailable)
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=TARGET - timedelta(hours=1)) == 1
    sender.assert_awaited_once()
    assert sender.await_args.args[0] == "g2"
    assert "仅限g1" not in sender.await_args.args[1]
