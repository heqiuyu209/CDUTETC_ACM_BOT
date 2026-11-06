"""通过真实 APScheduler 触发器验证预生成、跨周与启动提醒恢复。"""

import asyncio
from datetime import datetime, timedelta, timezone
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from cdutetc import config
from cdutetc import database
from cdutetc import main
from cdutetc import reminder
from cdutetc.errors import DeliveryMode, DeliveryResult


TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def memory_db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    database.init_db()
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"123456"})
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", {"123456"})
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    monkeypatch.setattr(config, "NL_INTENT_ENABLED", False)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_ENABLED", True)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_GROUPS", {"123456"})
    monkeypatch.setattr(config, "WEEKLY_CONTEST_WEEKDAY", 6)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_START", "19:00")
    monkeypatch.setattr(config, "WEEKLY_CONTEST_END", "21:00")
    monkeypatch.setattr(config, "REMIND_BEFORE_HOURS", 24)
    monkeypatch.setattr(config, "CONTEST_REMIND_OFFSETS_MINUTES", (30, 15))
    monkeypatch.setattr(config, "CONTEST_REMIND_GRACE_SECONDS", 120)
    monkeypatch.setattr(config, "CONTEST_REMIND_CHECK_SECONDS", 30)
    monkeypatch.setattr(database, "_delivery_now", lambda value=None: value or datetime(2026, 10, 31, 18, 29, tzinfo=TZ))
    monkeypatch.setattr(config, "REMIND_CHECK_INTERVAL_MINUTES", 30)
    monkeypatch.setattr(config, "REMIND_OFFSET_HOURS", (24, 1))
    yield conn
    conn.close()


def new_bot():
    return main.ACMBot(SimpleNamespace(send_group_msg_result=AsyncMock(
        return_value=DeliveryResult.sent("onebot", DeliveryMode.ACTIVE, "sent")
    )))


async def stop(bot):
    if bot.sched is not None:
        bot.sched.shutdown(wait=False)
        await asyncio.sleep(0)


async def test_actual_default_cron_generates_before_reminder_window(memory_db):
    bot = new_bot()
    before = datetime(2026, 10, 31, 18, 29, tzinfo=TZ)
    try:
        await bot._on_ready(now=before)
        job = bot.sched.get_job("weekly-contest")
        generation_at = job.trigger.get_next_fire_time(None, before)
        assert generation_at == datetime(2026, 10, 31, 18, 30, tzinfo=TZ)
        await job.func(now=generation_at)
        comp = database.list_competitions(status="confirmed")
        assert len(comp) == 1
        assert comp[0]["official_time"] == "2026-11-01 19:00"
        sender = AsyncMock(return_value=True)
        assert await reminder.check_general_reminders(sender, now=generation_at) == 0
        window_opens = generation_at + timedelta(minutes=30)
        assert await reminder.check_general_reminders(sender, now=window_opens) == 0
        sender.assert_not_awaited()
        plans = memory_db.execute(
            "SELECT scheduled_at, expires_at, group_id FROM reminder_deliveries"
            " WHERE obj_type='competition' AND remind_type='contest_start' ORDER BY scheduled_at"
        ).fetchall()
        assert plans == [
            ("2026-11-01 18:30:00", "2026-11-01 18:32:00", "123456"),
            ("2026-11-01 18:45:00", "2026-11-01 18:47:00", "123456"),
        ]
        first_node = datetime(2026, 11, 1, 18, 30, tzinfo=TZ)
        assert await reminder.check_contest_start_reminders(sender, now=first_node) == 1
        assert await reminder.check_contest_start_reminders(sender, now=first_node + timedelta(seconds=30)) == 0
        assert await reminder.check_contest_start_reminders(sender, now=first_node + timedelta(minutes=15)) == 1
        assert sender.await_count == 2
        assert all(call.args[0] == "123456" for call in sender.await_args_list)
    finally:
        await stop(bot)


async def test_startup_inside_last_scan_period_does_not_backfill_elapsed_nodes(memory_db, monkeypatch):
    now = datetime(2026, 11, 1, 18, 59, tzinfo=TZ)
    monkeypatch.setattr(database, "_delivery_now", lambda value=None: value or now)
    async def check_at_startup(sender):
        return await reminder.check_general_reminders(sender, now=now)
    async def check_contest_at_startup(sender):
        return await reminder.check_contest_start_reminders(sender, now=now)
    monkeypatch.setattr(main, "check_reminders", AsyncMock(return_value=0))
    monkeypatch.setattr(main, "check_general_reminders", check_at_startup)
    monkeypatch.setattr(main, "check_contest_start_reminders", check_contest_at_startup)
    bot = new_bot()
    restarted = new_bot()
    try:
        await bot._on_ready(now=now)
        remind_job = bot.sched.get_job("contest-remind-check")
        assert remind_job.next_run_time == now
        assert remind_job.trigger.interval == timedelta(seconds=30)
        await remind_job.func()
        await bot.sched.get_job("remind-check").func()
        bot.client.send_group_msg_result.assert_not_awaited()
        assert memory_db.execute(
            "SELECT COUNT(*) FROM reminder_deliveries WHERE remind_type='contest_start'"
        ).fetchone()[0] == 0
        await stop(bot)
        await restarted._on_ready(now=now)
        await restarted.sched.get_job("contest-remind-check").func()
        restarted.client.send_group_msg_result.assert_not_awaited()
        assert len(database.list_competitions()) == 1
    finally:
        if bot.sched is not None and bot.sched.running:
            await stop(bot)
        await stop(restarted)


async def test_generation_before_monday_contest_uses_next_week_key(memory_db, monkeypatch):
    monkeypatch.setattr(config, "WEEKLY_CONTEST_WEEKDAY", 0)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_START", "00:15")
    monkeypatch.setattr(config, "WEEKLY_CONTEST_END", "02:15")
    bot = new_bot()
    before = datetime(2026, 10, 31, 23, 44, tzinfo=TZ)
    try:
        await bot._on_ready(now=before)
        job = bot.sched.get_job("weekly-contest")
        generation_at = job.trigger.get_next_fire_time(None, before)
        assert generation_at == datetime(2026, 10, 31, 23, 45, tzinfo=TZ)
        await job.func(now=generation_at)
        comp = database.list_competitions()
        assert len(comp) == 1 and comp[0]["official_time"] == "2026-11-02 00:15"
        assert memory_db.execute("SELECT week_start FROM weekly_contest_runs").fetchone()[0] == "2026-11-02"
    finally:
        await stop(bot)


@pytest.mark.parametrize("clock", [
    datetime(2026, 11, 1, 19, 0, tzinfo=TZ),
    datetime(2026, 11, 1, 19, 30, tzinfo=TZ),
    datetime(2026, 11, 1, 11, 30, tzinfo=timezone.utc),
])
async def test_started_occurrence_not_created_during_recovery(memory_db, clock):
    bot = new_bot()
    await bot._weekly_contest_job(now=clock)
    comps = database.list_competitions()
    assert len(comps) == 1
    assert comps[0]["official_time"] == "2026-11-08 19:00"
    assert await reminder.check_general_reminders(AsyncMock(return_value=True), now=clock) == 0


async def test_repeated_ready_keeps_scheduler_and_week_record(memory_db):
    bot = new_bot()
    now = datetime(2026, 10, 31, 18, 29, tzinfo=TZ)
    try:
        await bot._on_ready(now=now)
        scheduler = bot.sched
        await bot._on_ready(now=now)
        assert bot.sched is scheduler
        assert {job.id for job in bot.sched.get_jobs()} == {
            "remind-check", "contest-remind-check", "source-crawl", "weekly-contest",
        }
        assert len(bot.sched.get_jobs()) == 4
        assert len(database.list_competitions()) == 1
    finally:
        await stop(bot)


async def test_disabled_weekly_does_not_create_startup_record_or_job(memory_db, monkeypatch):
    monkeypatch.setattr(config, "WEEKLY_CONTEST_ENABLED", False)
    bot = new_bot()
    try:
        await bot._on_ready(now=datetime(2026, 10, 31, 18, 29, tzinfo=TZ))
        assert bot.sched.get_job("weekly-contest") is None
        assert database.list_competitions() == []
    finally:
        await stop(bot)


async def test_running_scheduler_checks_reminders_without_waiting_first_interval(memory_db, monkeypatch):
    checked = asyncio.Event()
    monkeypatch.setattr(main, "check_reminders", AsyncMock(return_value=0))
    async def check_general(sender):
        checked.set()
        return 0
    monkeypatch.setattr(main, "check_general_reminders", check_general)
    bot = new_bot()
    try:
        await bot._on_ready()
        await asyncio.wait_for(checked.wait(), timeout=2)
        main.check_reminders.assert_awaited_once()
    finally:
        await stop(bot)
