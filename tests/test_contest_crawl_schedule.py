"""比赛抓取调度：每日一次 cron、默认 08:05、可用 ACM_CONTEST_CRAWL_TIME 覆盖。"""

import asyncio
import importlib
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from cdutetc import config, database, main
from cdutetc.errors import DeliveryMode, DeliveryResult

TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def memory_db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    database.init_db()
    fixed = datetime(2026, 11, 12, 9, 0, tzinfo=TZ)
    monkeypatch.setattr(database, "_delivery_now", lambda value=None: value or fixed)
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"123456"})
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", {"123456"})
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    monkeypatch.setattr(config, "NL_INTENT_ENABLED", False)
    monkeypatch.setattr(config, "DAILY_DIGEST_ENABLED", False)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_ENABLED", False)
    monkeypatch.setattr(config, "REMIND_CHECK_INTERVAL_MINUTES", 30)
    monkeypatch.setattr(config, "CONTEST_REMIND_CHECK_SECONDS", 30)
    monkeypatch.setattr(config, "REMIND_OFFSET_HOURS", (24, 1))
    monkeypatch.setattr(config, "CONTEST_REMIND_OFFSETS_MINUTES", (30, 15))
    yield conn
    conn.close()


def new_bot():
    return main.ACMBot(SimpleNamespace(send_group_msg_result=AsyncMock(
        return_value=DeliveryResult.sent("onebot", DeliveryMode.ACTIVE, "sent")
    )))


def _reload_env(monkeypatch, value):
    with monkeypatch.context() as env:
        if value is None:
            env.delenv("ACM_CONTEST_CRAWL_TIME", raising=False)
        else:
            env.setenv("ACM_CONTEST_CRAWL_TIME", value)
        importlib.reload(config)
        return config.CONTEST_CRAWL_HOUR, config.CONTEST_CRAWL_MINUTE


# ---------------------------------------------------------------- 配置解析

def test_contest_crawl_time_defaults_to_0805(monkeypatch):
    try:
        assert _reload_env(monkeypatch, None) == (8, 5)
    finally:
        importlib.reload(config)


@pytest.mark.parametrize("raw, expected", [
    ("07:30", (7, 30)),
    ("21:45", (21, 45)),
    ("00:00", (0, 0)),
    ("23:59", (23, 59)),
])
def test_contest_crawl_time_accepts_env_override(monkeypatch, raw, expected):
    try:
        assert _reload_env(monkeypatch, raw) == expected
    finally:
        importlib.reload(config)


@pytest.mark.parametrize("raw", ["abc", "25:00", "08:60", "8点05", ""])
def test_contest_crawl_time_falls_back_on_invalid(monkeypatch, raw):
    """非法配置只回退默认值，绝不让机器人起不来。"""
    try:
        assert _reload_env(monkeypatch, raw) == (8, 5)
    finally:
        importlib.reload(config)


# ---------------------------------------------------------------- 调度注册

async def test_source_crawl_job_is_daily_cron(memory_db, monkeypatch):
    """抓取任务必须是「每天固定时刻一次」的 cron，而不是分钟级 interval。"""
    monkeypatch.setattr(config, "CONTEST_CRAWL_HOUR", 8)
    monkeypatch.setattr(config, "CONTEST_CRAWL_MINUTE", 5)
    # 手动抓取频率下限调到 1 分钟：若还被它驱动，下一次触发就不会是明天
    monkeypatch.setattr(config, "FETCH_MIN_INTERVAL_MINUTES", 1)

    bot = new_bot()
    base = datetime(2026, 11, 12, 9, 0, tzinfo=TZ)  # 已经过了今天的 08:05
    try:
        await bot._on_ready(now=base)
        job = bot.sched.get_job("source-crawl")
        assert job is not None
        assert job.max_instances == 1 and job.coalesce is True
        fields = {field.name: str(field) for field in job.trigger.fields}
        assert fields["hour"] == "8" and fields["minute"] == "5"
        assert fields["day"] == "*" and fields["month"] == "*"
        assert job.trigger.get_next_fire_time(None, base) == datetime(2026, 11, 13, 8, 5, tzinfo=TZ)
        # 一整天只有一次触发
        again = job.trigger.get_next_fire_time(
            datetime(2026, 11, 13, 8, 5, tzinfo=TZ), datetime(2026, 11, 13, 8, 6, tzinfo=TZ)
        )
        assert again == datetime(2026, 11, 14, 8, 5, tzinfo=TZ)
        assert again - datetime(2026, 11, 13, 8, 5, tzinfo=TZ) == timedelta(days=1)
    finally:
        if bot.sched is not None:
            bot.sched.shutdown(wait=False)
            await asyncio.sleep(0)


async def test_source_crawl_job_follows_configured_clock(memory_db, monkeypatch):
    monkeypatch.setattr(config, "CONTEST_CRAWL_HOUR", 21)
    monkeypatch.setattr(config, "CONTEST_CRAWL_MINUTE", 40)

    bot = new_bot()
    base = datetime(2026, 11, 12, 9, 0, tzinfo=TZ)
    try:
        await bot._on_ready(now=base)
        job = bot.sched.get_job("source-crawl")
        assert job.trigger.get_next_fire_time(None, base) == datetime(2026, 11, 12, 21, 40, tzinfo=TZ)
    finally:
        if bot.sched is not None:
            bot.sched.shutdown(wait=False)
            await asyncio.sleep(0)
