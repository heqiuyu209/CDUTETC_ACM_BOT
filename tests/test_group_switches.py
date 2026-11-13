"""群内细粒度后台开关（/群开关）：面板、权限、持久化与推送门控。"""

import sqlite3
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from cdutetc import commands, config, database, daily_digest, main
from cdutetc.clients.onebot import OneBotMessage

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 12, 10, 0, tzinfo=TZ)  # 周四


def _msg(content="你好", group="g1", member="m1"):
    return OneBotMessage({
        "post_type": "message", "message_type": "group",
        "raw_message": content, "group_id": group, "user_id": member,
        "message_id": 12345, "self_id": 30001,
    })


@pytest.fixture
def group_db(monkeypatch):
    """内存库 + 固定时钟 + 白名单身份，隔离真实数据与网络。"""
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    monkeypatch.setattr(database, "_delivery_now", lambda value=None: value or NOW)
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    database.init_db()
    yield conn
    conn.close()


# ---------------------------------------------------------------- 面板渲染

def test_panel_lists_all_four_switches_on_by_default(group_db):
    text = commands.render_group_switches("g1")
    assert "本群主动能力开关" in text
    for label in ("问答", "比赛提醒", "课程提醒", "晨报"):
        assert f"{label}：开启" in text
    assert "/群开关" in text


def test_panel_reflects_closed_switch(group_db):
    commands.cmd_group_switch("g1", "teacher", "比赛提醒 关闭")
    text = commands.render_group_switches("g1")
    assert "比赛提醒：关闭" in text
    assert "问答：开启" in text


# ---------------------------------------------------------------- 权限

def test_switch_command_rejects_plain_member(group_db):
    replies = commands.cmd_group_switch("g1", "m1", "问答 关闭")
    assert "仅限本群老师或超级管理员" in replies[0]["text"]
    # 未被越权改动
    assert database.group_switch_enabled("g1", "qa_enabled") is True


def test_switch_command_allows_super_admin(group_db):
    replies = commands.cmd_group_switch("g1", "admin", "晨报 关闭")
    assert "本群晨报已关闭" in replies[0]["text"]
    assert database.group_switch_enabled("g1", "daily_digest_enabled") is False


# ---------------------------------------------------------------- 修改生效

def test_switch_off_persists_and_appends_hint(group_db):
    replies = commands.cmd_group_switch("g1", "teacher", "问答 关闭")
    text = replies[0]["text"]
    assert "本群问答已关闭" in text
    assert "仍可发送 /命令" in text
    assert database.get_group_switches("g1")["qa_enabled"] is False


def test_switch_on_again_clears_state_without_hint(group_db):
    commands.cmd_group_switch("g1", "teacher", "问答 关闭")
    replies = commands.cmd_group_switch("g1", "teacher", "问答 开启")
    assert "本群问答已开启" in replies[0]["text"]
    assert "关闭后" not in replies[0]["text"]
    assert database.group_switch_enabled("g1", "qa_enabled") is True


def test_course_alias_syncs_legacy_setting_key(group_db):
    commands.cmd_group_switch("g1", "teacher", "课程 关闭")
    assert database.group_switch_enabled("g1", "course_remind_enabled") is False
    raw = database.get_group_setting("g1", database.COURSE_REMIND_SETTING, "")
    assert str(raw).lower() in ("off", "0", "false")


def test_switch_state_is_per_group(group_db):
    commands.cmd_group_switch("g1", "teacher", "问答 关闭")
    assert database.group_switch_enabled("g1", "qa_enabled") is False
    # 其他群缺省仍开启
    assert database.group_switch_enabled("g2", "qa_enabled") is True


@pytest.mark.parametrize("args", ["不认识的开关 关闭", "问答 打开", "问答"])
def test_invalid_switch_arguments_raise_usage_error(group_db, args):
    with pytest.raises(ValueError):
        commands.cmd_group_switch("g1", "teacher", args)


# ---------------------------------------------------------------- 问答门控

@pytest.fixture
def gate_bot(monkeypatch, onebot_client):
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "GROUP_REQUIRE_AT", True)
    monkeypatch.setattr(config, "NL_INTENT_ENABLED", False)
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    database.init_db()
    bot = main.ACMBot(onebot_client)
    replies = []

    async def fake_reply(message, content):
        replies.append(content)

    monkeypatch.setattr(bot, "_safe_reply", fake_reply)
    yield bot, replies, conn
    conn.close()


async def test_qa_switch_off_silently_blocks_member_free_text(gate_bot, monkeypatch):
    bot, replies, _ = gate_bot
    handler = AsyncMock(return_value=[{"group_id": "g1", "text": "自动回答"}])
    monkeypatch.setattr(commands, "handle_non_command_message", handler)
    database.set_group_switch("g1", "qa_enabled", False, "teacher")

    await bot.on_group_at_message_create(_msg("[CQ:at,qq=30001] 今天有什么比赛"))

    handler.assert_not_awaited()
    assert replies == []


async def test_qa_switch_on_answers_member_free_text(gate_bot, monkeypatch):
    bot, replies, _ = gate_bot
    handler = AsyncMock(return_value=[{"group_id": "g1", "text": "自动回答"}])
    monkeypatch.setattr(commands, "handle_non_command_message", handler)

    await bot.on_group_at_message_create(_msg("[CQ:at,qq=30001] 今天有什么比赛"))

    handler.assert_awaited_once()
    assert replies == ["自动回答"]


async def test_qa_switch_off_still_allows_teacher_commands(gate_bot, monkeypatch):
    bot, replies, _ = gate_bot
    handler = AsyncMock(return_value=[{"group_id": "g1", "text": "不该走到这"}])
    monkeypatch.setattr(commands, "handle_non_command_message", handler)
    database.set_group_switch("g1", "qa_enabled", False, "teacher")

    await bot.on_group_at_message_create(_msg("[CQ:at,qq=30001] /群开关", member="teacher"))

    handler.assert_not_awaited()
    assert any("本群主动能力开关" in text for text in replies)


# ---------------------------------------------------------------- 晨报推送门控

def _digest_detail(group_id, now=None):
    """晨报收集结果的最小替身：有内容、有今日比赛。"""
    return {
        "text": "晨报内容",
        "today_contest_count": 1,
        "contest_enabled": True,
        "section_count": 1,
    }


async def test_daily_digest_skips_group_when_switch_off(gate_bot, monkeypatch):
    bot, _, _ = gate_bot
    monkeypatch.setattr(config, "DAILY_DIGEST_ENABLED", True)
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    monkeypatch.setattr(daily_digest, "collect_daily_digest", _digest_detail)
    sent = AsyncMock(return_value=SimpleNamespace(ok=True, code=None))
    monkeypatch.setattr(bot, "_send_group_result", sent)
    database.set_group_switch("g1", "daily_digest_enabled", False, "teacher")

    assert await bot._daily_digest_job() == 0
    sent.assert_not_awaited()


async def test_daily_digest_sends_when_switch_on(gate_bot, monkeypatch):
    bot, _, _ = gate_bot
    monkeypatch.setattr(config, "DAILY_DIGEST_ENABLED", True)
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    monkeypatch.setattr(daily_digest, "collect_daily_digest", _digest_detail)
    sent = AsyncMock(return_value=SimpleNamespace(ok=True, code=None))
    monkeypatch.setattr(bot, "_send_group_result", sent)

    assert await bot._daily_digest_job() == 1
    sent.assert_awaited_once_with("g1", "晨报内容")
