"""业务入口按官方协议显式区分主动与被动，无真实 QQ/API。"""
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cdutetc import config, database, main
from cdutetc.clients.qqbot import QQBotClient, QQBotMessage
from cdutetc.errors import DeliveryCode


@pytest.fixture
def official_bot(monkeypatch):
    monkeypatch.setattr(config, "BOT_PROTOCOL", "qqbot")
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"group-openid"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin-openid"})
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_ENABLED", False)
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    database.init_db()
    api = SimpleNamespace(
        post_group_message=AsyncMock(return_value={"id": "sent-group-id"}),
        post_c2c_message=AsyncMock(return_value={"id": "sent-private-id"}),
    )
    client = QQBotClient("dummy-app", "dummy-secret")
    client._client = SimpleNamespace(api=api)
    yield main.ACMBot(client), api
    conn.close()


def group_message(msg_id="inbound-id"):
    return QQBotMessage(None, message_type="group", group_id="group-openid",
                        user_id="admin-openid", msg_id=msg_id, always_addressed=True)


async def test_direct_reply_uses_current_id_and_incrementing_seq(official_bot):
    bot, api = official_bot
    token = main._reply_sequence.set(0)
    try:
        assert await bot._safe_reply(group_message(), "第一条")
        assert await bot._safe_reply(group_message(), "第二条")
    finally:
        main._reply_sequence.reset(token)
    first, second = api.post_group_message.await_args_list
    assert first.kwargs["msg_id"] == second.kwargs["msg_id"] == "inbound-id"
    assert [first.kwargs["msg_seq"], second.kwargs["msg_seq"]] == [1, 2]


async def test_active_notice_never_reuses_inbound_id(official_bot):
    bot, api = official_bot
    assert await bot._safe_reply(group_message(), "被动回复")
    assert await bot._send_group_result("group-openid", "主动提醒")
    assert "msg_id" not in api.post_group_message.await_args.kwargs
    assert 1 <= api.post_group_message.await_args.kwargs["msg_seq"] <= 2**31 - 1


async def test_missing_inbound_id_does_not_fall_back_to_active(official_bot):
    bot, api = official_bot
    assert not await bot._safe_reply(group_message(""), "不应主动发出")
    api.post_group_message.assert_not_awaited()


async def test_active_group_gate_returns_error_without_network(official_bot, monkeypatch):
    bot, api = official_bot
    result = await bot._send_group_result("other-group", "消息")
    assert result.code == DeliveryCode.TARGET_NOT_ALLOWED
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", False)
    result = await bot._send_group_result("group-openid", "消息")
    assert result.code == DeliveryCode.DISABLED
    api.post_group_message.assert_not_awaited()


async def test_private_reply_context_is_reset_and_test_is_active(official_bot):
    bot, api = official_bot
    msg = QQBotMessage(None, message_type="private", user_id="admin-openid",
                       msg_id="private-inbound", content="/通知测试 私聊")
    await bot.on_private_message_create(msg)
    active, reply = api.post_c2c_message.await_args_list
    assert "msg_id" not in active.kwargs
    assert reply.kwargs["msg_id"] == "private-inbound"
    assert reply.kwargs["msg_seq"] == 1
    assert "[sent]" in reply.kwargs["content"]
    assert main._private_reply_context.get() is None
    await bot._send_private_result("admin-openid", "之后的主动通知")
    assert "msg_id" not in api.post_c2c_message.await_args.kwargs


async def test_non_admin_cannot_trigger_notification_test(official_bot):
    bot, api = official_bot
    answer = await bot._notification_test("member", "群 group-openid")
    assert "permission" in answer
    api.post_group_message.assert_not_awaited()
    api.post_c2c_message.assert_not_awaited()


async def test_contest_job_uses_structured_result_callback(official_bot, monkeypatch):
    bot, api = official_bot
    check = AsyncMock(return_value=0)
    monkeypatch.setattr(main, "check_contest_start_reminders", check)
    await bot._contest_remind_job()
    check.assert_awaited_once_with(bot._send_group_result)
