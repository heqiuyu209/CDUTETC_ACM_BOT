"""OneBot 群消息触发范围：命令与定向提问，拒绝闲聊和发给他人的消息。"""

import logging
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cdutetc import config
from cdutetc import database
from cdutetc import main
from cdutetc.clients.onebot import OneBotMessage
from cdutetc.errors import DeliveryMode, DeliveryResult
from cdutetc.utils import mentioned_user_ids


def message(content, *, group=123456, user=654321, self_id=111111):
    return OneBotMessage({
        "post_type": "message", "message_type": "group", "message_id": 1,
        "group_id": group, "user_id": user, "self_id": self_id,
        "raw_message": content,
    })


@pytest.fixture
def bot(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"123456", "234567"})
    monkeypatch.setattr(config, "BOT_QQ", "111111")
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"654321"})
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    monkeypatch.setattr(config, "GROUP_REQUIRE_AT", True)
    monkeypatch.setattr(config, "NL_INTENT_ENABLED", False)
    monkeypatch.setattr(config, "QA_WHITELIST_ENABLED", False)
    monkeypatch.setattr(config, "QA_WHITELIST", set())
    monkeypatch.setattr(config, "QA_BLACKLIST", set())
    monkeypatch.setattr(config, "QA_RATE_LIMIT_ENABLED", False)
    client = SimpleNamespace(
        send_group_msg_result=AsyncMock(return_value=DeliveryResult.sent("onebot", DeliveryMode.ACTIVE, "sent")),
        send_private_msg_result=AsyncMock(return_value=DeliveryResult.sent("onebot", DeliveryMode.ACTIVE, "sent")),
    )
    return main.ACMBot(client)


@pytest.mark.parametrize("content", [
    "今天食堂有什么菜", "看看这个 https://example.com/contest",
    "[CQ:at,qq=999999] 下课了吗", "[CQ:at,qq=999999] /帮助",
    "[CQ:at,qq=all] 近期比赛是什么", "[CQ:at,qq=1111110] 近期比赛是什么",
])
async def test_unaddressed_messages_have_no_side_effects(bot, monkeypatch, content):
    command = AsyncMock()
    question = AsyncMock()
    publish = AsyncMock()
    monkeypatch.setattr(main.commands, "handle_command", command)
    monkeypatch.setattr(main.commands, "handle_non_command_message", question)
    monkeypatch.setattr(bot, "_handle_publish", publish)

    await bot.on_group_at_message_create(message(content))

    command.assert_not_awaited()
    question.assert_not_awaited()
    publish.assert_not_awaited()
    bot.client.send_group_msg_result.assert_not_awaited()


@pytest.mark.parametrize("content", ["[CQ:at,qq=111111] /帮助", "<@!111111> /帮助"])
async def test_addressed_explicit_commands_preserve_group_and_sender(bot, monkeypatch, content):
    command = AsyncMock(return_value=[{"group_id": "123456", "text": "帮助文本"}])
    monkeypatch.setattr(main.commands, "handle_command", command)
    await bot.on_group_at_message_create(message(content))
    command.assert_awaited_once_with("/帮助", "123456", "654321")
    bot.client.send_group_msg_result.assert_awaited_once_with("123456", "帮助文本", reply_to="1")


async def test_default_group_gate_rejects_unmentioned_explicit_command(bot, monkeypatch):
    command = AsyncMock()
    monkeypatch.setattr(main.commands, "handle_command", command)

    await bot.on_group_at_message_create(message("/帮助"))

    command.assert_not_awaited()
    bot.client.send_group_msg_result.assert_not_awaited()


async def test_disabled_group_gate_allows_unmentioned_explicit_command_with_reply(bot, monkeypatch):
    monkeypatch.setattr(config, "GROUP_REQUIRE_AT", False)
    command = AsyncMock(return_value=[{"group_id": "123456", "text": "帮助文本"}])
    monkeypatch.setattr(main.commands, "handle_command", command)

    await bot.on_group_at_message_create(message("/帮助"))

    command.assert_awaited_once_with("/帮助", "123456", "654321")
    bot.client.send_group_msg_result.assert_awaited_once_with("123456", "帮助文本", reply_to="1")


async def test_disabled_group_gate_does_not_allow_unmentioned_question(bot, monkeypatch):
    monkeypatch.setattr(config, "GROUP_REQUIRE_AT", False)
    question = AsyncMock()
    monkeypatch.setattr(main.commands, "handle_non_command_message", question)

    await bot.on_group_at_message_create(message("最近比赛是什么"))

    question.assert_not_awaited()
    bot.client.send_group_msg_result.assert_not_awaited()


async def test_question_handler_receives_staff_permission_keyword(bot, monkeypatch):
    command = AsyncMock(return_value=None)
    question = AsyncMock(return_value=[{"group_id": "123456", "text": "答复"}])
    monkeypatch.setattr(main.commands, "handle_command", command)
    monkeypatch.setattr(main.commands, "handle_non_command_message", question)

    await bot.on_group_at_message_create(message("[CQ:at,qq=111111] 近期比赛是什么"))

    question.assert_awaited_once_with("近期比赛是什么", "123456", "654321", staff=True)
    bot.client.send_group_msg_result.assert_awaited_once_with("123456", "答复", reply_to="1")


async def test_only_addressed_questions_reach_model_and_question_storage(bot, monkeypatch):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    database.init_db()
    answer = AsyncMock(return_value="模型暂时不可用，请稍后再试~")
    monkeypatch.setattr(main.commands.llm, "answer_question", answer)
    try:
        await bot.on_group_at_message_create(message("今天食堂有什么菜"))
        await bot.on_group_at_message_create(message("[CQ:at,qq=999999] 下课了吗"))
        await bot.on_group_at_message_create(message("[CQ:at,qq=111111] 近期比赛是什么"))
        answer.assert_awaited_once()
        assert answer.await_args.args[0] == "近期比赛是什么"
        questions = database.list_unanswered("123456")
        assert [q["question"] for q in questions] == ["近期比赛是什么"]
        assert bot.client.send_group_msg_result.await_count == 1
    finally:
        conn.close()


async def test_missing_configured_id_uses_onebot_self_id(bot, monkeypatch):
    monkeypatch.setattr(config, "BOT_QQ", "")
    command = AsyncMock(return_value=None)
    question = AsyncMock(return_value=[{"group_id": "123456", "text": "回答"}])
    monkeypatch.setattr(main.commands, "handle_command", command)
    monkeypatch.setattr(main.commands, "handle_non_command_message", question)
    await bot.on_group_at_message_create(message("[CQ:at,qq=111111] 时间是什么"))
    question.assert_awaited_once_with("时间是什么", "123456", "654321", staff=True)


async def test_missing_bot_identity_ignores_mentions_but_allows_legacy_commands(bot, monkeypatch):
    monkeypatch.setattr(config, "BOT_QQ", "")
    command = AsyncMock(return_value=[])
    question = AsyncMock()
    monkeypatch.setattr(main.commands, "handle_command", command)
    monkeypatch.setattr(main.commands, "handle_non_command_message", question)

    await bot.on_group_at_message_create(message("[CQ:at,qq=111111] 时间是什么", self_id=None))
    command.assert_not_awaited()
    question.assert_not_awaited()

    monkeypatch.setattr(config, "GROUP_REQUIRE_AT", False)
    await bot.on_group_at_message_create(message("/帮助", self_id=None))
    command.assert_awaited_once_with("/帮助", "123456", "654321")


@pytest.mark.parametrize("enabled, expected_calls", [(False, 0), (True, 1)])
async def test_unallowed_group_only_receives_optional_authorization_notice(
    bot, monkeypatch, enabled, expected_calls,
):
    monkeypatch.setattr(config, "NOTICE_UNAUTHORIZED_GROUP", enabled)
    command = AsyncMock()
    question = AsyncMock()
    monkeypatch.setattr(main.commands, "handle_command", command)
    monkeypatch.setattr(main.commands, "handle_non_command_message", question)

    await bot.on_group_at_message_create(message("[CQ:at,qq=111111] /帮助", group=345678))

    command.assert_not_awaited()
    question.assert_not_awaited()
    assert bot.client.send_group_msg_result.await_count == expected_calls
    if enabled:
        assert bot.client.send_group_msg_result.await_args.args[0] == "345678"
        assert "尚未授权" in bot.client.send_group_msg_result.await_args.args[1]


async def test_unmentioned_publish_keeps_teacher_permission_check(bot, monkeypatch):
    monkeypatch.setattr(config, "GROUP_REQUIRE_AT", False)
    fetch = AsyncMock()
    monkeypatch.setattr(main, "fetch_text", fetch)
    await bot.on_group_at_message_create(message("发布 https://example.com/contest", user=999999))
    fetch.assert_not_awaited()
    bot.client.send_group_msg_result.assert_awaited_once()
    assert "没有发布" in bot.client.send_group_msg_result.await_args.args[1]


async def test_publish_without_mention_routes_to_publish_handler(bot, monkeypatch):
    monkeypatch.setattr(config, "GROUP_REQUIRE_AT", False)
    publish = AsyncMock()
    monkeypatch.setattr(bot, "_handle_publish", publish)
    event = message("发布 https://example.com/contest")
    await bot.on_group_at_message_create(event)
    publish.assert_awaited_once_with(event, "https://example.com/contest", "123456", "654321")


async def test_command_cross_group_replies_still_use_target_group(bot, monkeypatch):
    monkeypatch.setattr(config, "GROUP_REQUIRE_AT", False)
    monkeypatch.setattr(main.commands, "handle_command", AsyncMock(return_value=[
        {"group_id": "123456", "text": "源群"},
        {"group_id": "234567", "text": "目标群"},
    ]))
    await bot.on_group_at_message_create(message("/帮助"))
    calls = [call.args for call in bot.client.send_group_msg_result.await_args_list]
    assert ("234567", "目标群") in calls
    assert calls[-1][0] == "123456" and "群通知已发送" in calls[-1][1]


async def test_original_group_and_private_content_not_logged(bot, caplog):
    with caplog.at_level(logging.INFO, logger="main"):
        await bot.on_group_at_message_create(message("私密群聊原文"))
        await bot.on_private_message_create(message("私密私聊原文"))
    assert "私密群聊原文" not in caplog.text
    assert "私密私聊原文" not in caplog.text
    assert "[身份采集]" not in caplog.text


def test_mentions_match_exact_ids_and_allow_cq_extra_parameters():
    assert mentioned_user_ids("[CQ:at,qq=111111,name=老师] <@!222222>") == {"111111", "222222"}
    assert mentioned_user_ids("&#91;CQ:at,qq=111111&#93;") == set()
