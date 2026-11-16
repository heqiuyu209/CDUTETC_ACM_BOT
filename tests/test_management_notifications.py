"""Offline regression tests for management command notifications."""

import sqlite3
from unittest.mock import AsyncMock

import pytest

from cdutetc import config, database, main
from conftest import official_message
from cdutetc.errors import DeliveryCode, DeliveryMode, DeliveryResult


@pytest.fixture
def management_bot(monkeypatch, official_client):
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1"})
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", set())
    monkeypatch.setattr(config, "ENV_TEACHER_WHITELIST", set())
    monkeypatch.setattr(config, "TEACHER_WHITELIST", set())
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin-1"})
    monkeypatch.setattr(config, "GROUP_REQUIRE_AT", True)

    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    database.init_db()

    bot = main.ACMBot(official_client)
    group_replies = []

    async def fake_reply(message, text):
        group_replies.append(text)

    monkeypatch.setattr(bot, "_safe_reply", fake_reply)
    yield bot, group_replies, official_client
    conn.close()


def _admin_command(text, member="admin-1"):
    return official_message(message_type="group", group_id="g1", user_id=member, content=f"<@30001> {text}", msg_id=1, bot_id=30001)


def _failure():
    return DeliveryResult(False, DeliveryCode.NOT_CONNECTED, "qqbot", DeliveryMode.ACTIVE)


@pytest.mark.parametrize(
    ("command", "table", "identity_column", "identity"),
    [
        ("/加群 openid-123456", "runtime_allowed_groups", "group_id", "openid-123456"),
        ("/加老师 openid-123456 负责训练", "runtime_teachers", "user_id", "openid-123456"),
    ],
)
async def test_management_add_notifies_super_admin_and_persists(
    management_bot, command, table, identity_column, identity,
):
    bot, group_replies, client = management_bot
    client.send_private_msg_result = AsyncMock(return_value=DeliveryResult.sent("qqbot", DeliveryMode.ACTIVE, "sent-id"))
    await bot.on_group_at_message_create(_admin_command(command))

    client.send_private_msg_result.assert_awaited_once()
    admin, text = client.send_private_msg_result.await_args.args[:2]
    assert admin == "admin-1"
    assert "管理变更确认" in text
    assert "openid-123456" in text
    assert any("已添加" in reply for reply in group_replies)

    row = database._connect().execute(
        f"SELECT {identity_column} FROM {table} WHERE {identity_column} = ?", (identity,)
    ).fetchone()
    assert row is not None


async def test_management_notification_failure_is_visible_without_rollback(management_bot):
    bot, group_replies, client = management_bot
    client.send_private_msg_result = AsyncMock(return_value=_failure())

    await bot.on_group_at_message_create(_admin_command("/加群 openid-123456"))

    assert any("未能私聊确认超管" in reply for reply in group_replies)
    assert database._connect().execute(
        "SELECT 1 FROM runtime_allowed_groups WHERE group_id = ?", ("openid-123456",)
    ).fetchone() is not None


async def test_management_notification_partial_failure(management_bot, monkeypatch):
    bot, replies, client = management_bot
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin-1", "admin-2"})
    client.send_private_msg_result = AsyncMock(side_effect=[
        DeliveryResult.sent("qqbot", DeliveryMode.ACTIVE, "sent-1"), _failure(),
    ])
    await bot.on_group_at_message_create(_admin_command("/加老师 openid-123456"))
    assert client.send_private_msg_result.await_count == 2
    assert any("1/2" in reply for reply in replies)
    assert database._connect().execute(
        "SELECT 1 FROM runtime_teachers WHERE user_id = ?", ("openid-123456",)
    ).fetchone() is not None


async def test_unauthorized_management_does_not_notify(management_bot):
    bot, replies, client = management_bot
    client.send_private_msg_result = AsyncMock(return_value=DeliveryResult.sent("qqbot", DeliveryMode.ACTIVE, "sent-id"))
    await bot.on_group_at_message_create(_admin_command("/加群 openid-123456", member="student"))
    client.send_private_msg_result.assert_not_awaited()
    assert any("仅超管" in reply for reply in replies)


async def test_publish_review_delivery_failure_is_reported_without_losing_request(
    management_bot, monkeypatch,
):
    bot, group_replies, client = management_bot
    monkeypatch.setattr(config, "PUBLISH_CONFIRM_ENABLED", True)
    client.send_private_msg_result = AsyncMock(return_value=_failure())

    async def no_fetch(url):
        raise AssertionError("test uses plain text and must not fetch")

    async def extract(text):
        return ({"name": "离线回归比赛"}, None)

    monkeypatch.setattr(main, "fetch_text", no_fetch)
    monkeypatch.setattr(main, "extract_contest", extract)
    await bot.on_group_at_message_create(_admin_command("发布 离线回归比赛"))

    pending = database.list_pending_publications()
    assert len(pending) == 1
    assert pending[0]["name"] == "离线回归比赛"
    assert any("待超管确认" in reply and "私聊推送失败" in reply for reply in group_replies)
