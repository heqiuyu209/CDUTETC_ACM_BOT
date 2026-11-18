"""每日清理独立静默执行、失败隔离及北京时间调度。"""

import asyncio
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

import cdutetc
from cdutetc import config, database as db, llm, main


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "cleanup_schedule.sqlite3"))
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"team"})
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", {"team"})
    monkeypatch.setattr(config, "WEEKLY_CONTEST_ENABLED", False)
    monkeypatch.setattr(config, "PERSONA_CHAT_ENABLED", False)
    monkeypatch.setattr(config, "AUTO_CONTEST_CRAWL_ENABLED", False)
    monkeypatch.setattr(config, "DAILY_DIGEST_ENABLED", False)
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", False)
    db.init_db()
    cleanup = SimpleNamespace(run_rules=Mock(return_value={"team": {"cleared": {"competition": 1}}}))
    monkeypatch.setattr(cdutetc, "group_cleanup", cleanup, raising=False)
    monkeypatch.setitem(sys.modules, "cdutetc.group_cleanup", cleanup)
    chat = AsyncMock(side_effect=AssertionError("清理不得调用模型"))
    monkeypatch.setattr(llm, "_chat_once", chat)
    bot = main.ACMBot(SimpleNamespace())
    bot._send_group_result = AsyncMock(side_effect=AssertionError("清理不得发群消息"))
    bot._send_private_result = AsyncMock(side_effect=AssertionError("清理不得发私聊消息"))
    bot._general_remind_job = AsyncMock()
    bot._contest_remind_job = AsyncMock()
    bot._crawl_job = AsyncMock()
    return SimpleNamespace(bot=bot, cleanup=cleanup, chat=chat)


async def test_daily_job_runs_with_active_messages_disabled_and_stays_silent(env):
    result = await env.bot._group_cleanup_job()
    env.cleanup.run_rules.assert_called_once_with()
    assert result["team"]["cleared"]["competition"] == 1
    env.bot._send_group_result.assert_not_awaited()
    env.bot._send_private_result.assert_not_awaited()
    env.chat.assert_not_awaited()


async def test_daily_job_backend_failure_is_isolated(env):
    env.cleanup.run_rules.side_effect = RuntimeError("backup failed")
    assert await env.bot._group_cleanup_job() == {}
    env.bot._send_group_result.assert_not_awaited()
    env.bot._send_private_result.assert_not_awaited()
    env.chat.assert_not_awaited()


async def test_cleanup_cron_uses_beijing_and_ready_is_idempotent(env, monkeypatch):
    monkeypatch.setattr(config, "TZ_NAME", "UTC")
    now = datetime(2026, 11, 18, 3, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    try:
        await env.bot._on_ready(now=now)
        scheduler = env.bot.sched
        await env.bot._on_ready(now=now)
        assert env.bot.sched is scheduler
        job = scheduler.get_job("group-cleanup")
        assert job is not None and job.max_instances == 1 and job.coalesce is True
        assert job.trigger.get_next_fire_time(None, now) == datetime(
            2026, 11, 18, 4, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
        assert len([job for job in scheduler.get_jobs() if job.id == "group-cleanup"]) == 1
    finally:
        if env.bot.sched is not None:
            env.bot.sched.shutdown(wait=False)
            await asyncio.sleep(0)
