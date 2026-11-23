"""无 @ 插话不得抢业务问答、复读离题兜底或代用户报告到场。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cdutetc import commands, config, main, persona_chat, private_chat
from cdutetc.clients.qqbot import QQBotClient, QQBotMessage
from cdutetc.db import connection
from cdutetc.errors import DeliveryCode, DeliveryMode, DeliveryResult


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(connection, "DB_PATH", str(tmp_path / "interjection.sqlite3"))
    for key, value in {
        "ALLOWED_GROUPS": {"g1"}, "SUPER_ADMIN": {"admin"}, "TEACHER_WHITELIST": set(),
        "TEACHER_AUTO_INTENT_ENABLED": False, "NL_INTENT_ENABLED": False,
        "GROUP_REQUIRE_AT": True, "PERSONA_CHAT_ENABLED": True, "QA_SOCIAL_CHAT_ENABLED": True,
        "QA_BLACKLIST": set(), "QA_WHITELIST_ENABLED": False, "QA_RATE_LIMIT_ENABLED": False,
        "MEME_ENABLED": False, "ECHO_ENABLED": False, "LLM_API_KEY": "offline-key",
    }.items():
        monkeypatch.setattr(config, key, value)
    main.db.init_db()
    persona_chat.reset_engine()
    gate = persona_chat.InterjectionGate(min_messages=1, probability=1, clock=lambda: 1000)
    monkeypatch.setattr(persona_chat, "_default_gate", gate)
    api = SimpleNamespace(post_group_message=AsyncMock(return_value={"id": "offline-sent"}))
    client = QQBotClient("offline-app", "offline-secret")
    client._client = SimpleNamespace(api=api)
    bot = main.ACMBot(client)
    model = AsyncMock(return_value="顺口接一句。")
    monkeypatch.setattr(persona_chat, "chat", model)
    non_command = AsyncMock(side_effect=AssertionError("无 @ 插话不得进入问答离题兜底"))
    monkeypatch.setattr(commands, "handle_non_command_message", non_command)
    yield SimpleNamespace(bot=bot, api=api, model=model, gate=gate, non_command=non_command)
    persona_chat.reset_engine()


def _message(text, mid="group-current", addressed=False):
    return QQBotMessage(None, message_type="group", group_id="g1", user_id="member",
                        msg_id=mid, content=text,
                        mentions=[{"id": "bot", "is_you": True}] if addressed else [])


@pytest.mark.parametrize("text", ["校赛地点", "今天集训取消了吗", "你在哪里", "帮我修改这条课程"])
async def test_unaddressed_information_and_business_do_not_interject_or_enter_memory(app, text):
    await app.bot.on_group_at_message_create(_message(text))
    app.model.assert_not_awaited()
    app.api.post_group_message.assert_not_awaited()
    app.non_command.assert_not_awaited()
    assert persona_chat.get_memory().recent("g1") == []


async def test_unaddressed_slash_does_not_trigger_persona_or_offtopic(app):
    await app.bot.on_group_at_message_create(_message("/帮助"))
    app.model.assert_not_awaited()
    app.api.post_group_message.assert_not_awaited()
    app.non_command.assert_not_awaited()


async def test_arrival_statement_is_not_repeated_as_offtopic_reply(app):
    app.model.return_value = "我只答 ACM 集训队的问题，换个正经点的问。"
    await app.bot.on_group_at_message_create(_message("到了"))
    assert not any("我只答 ACM" in call.kwargs["content"] for call in app.api.post_group_message.await_args_list)
    app.non_command.assert_not_awaited()


async def test_arrival_statement_does_not_make_bot_claim_physical_arrival(app):
    app.model.return_value = "我到了，已经到机房了。"
    await app.bot.on_group_at_message_create(_message("到了"))
    assert not any("我到了" in call.kwargs["content"] for call in app.api.post_group_message.await_args_list)


async def test_successful_group_reply_marks_bot_spoke_for_other_entertainment(app):
    assert await app.bot._safe_reply(_message("你好"), "收到，你接着说。")
    assert app.gate._spoke_at.get("g1") == 1000


async def test_failed_group_reply_is_not_remembered_as_bot_spoke(app, monkeypatch):
    monkeypatch.setattr(app.bot.client, "send_group_msg_result", AsyncMock(return_value=DeliveryResult(
        False, DeliveryCode.PLATFORM_ERROR, "qqbot", DeliveryMode.PASSIVE)))
    assert not await app.bot._safe_reply(_message("你好"), "收到，你接着说。")
    assert not app.gate._spoke_at


async def test_model_failure_during_interjection_does_not_report_bot_spoke(app):
    app.model.side_effect = RuntimeError("offline model failure")
    assert not await app.bot._maybe_persona_interject(_message("今天真开心"), "今天真开心", "g1")
    app.api.post_group_message.assert_not_awaited()
    assert not app.gate._spoke_at


async def test_duplicate_gateway_delivery_does_not_repeat_interjection(app):
    incoming = _message("今天真开心")
    await app.bot.client._receive_group_message(incoming)
    await app.bot.client._receive_group_message(incoming)
    app.model.assert_awaited_once()
    app.api.post_group_message.assert_awaited_once()
    assert app.api.post_group_message.await_args.kwargs["msg_id"] == "group-current"


@pytest.mark.parametrize("reply", ["我只想喝奶茶，今天太累了。", "这题又红温了。", "人齐了就开冲。"])
async def test_valid_casual_interjection_still_sends_one_reply(app, reply):
    app.model.return_value = reply
    await app.bot.on_group_at_message_create(_message("今天真开心"))
    app.model.assert_awaited_once()
    app.api.post_group_message.assert_awaited_once()
    assert app.api.post_group_message.await_args.kwargs["content"] == reply
    assert app.gate._spoke_at.get("g1") == 1000
    assert f"[学姐]: {reply}" in persona_chat.get_memory().recent("g1")


@pytest.mark.parametrize("reply", [
    "我只回答与ACM集训队比赛训练相关的问题哦～", "我已经到场了", "到了机房啦",
    "我在机房了", "刚到现场了", "我只会回答ACM相关问题",
])
async def test_forbidden_model_reply_is_not_sent_or_remembered_as_bot_spoke(app, reply):
    app.model.return_value = reply
    assert not await app.bot._maybe_persona_interject(_message("今天真开心"), "今天真开心", "g1")
    app.model.assert_awaited_once()
    app.api.post_group_message.assert_not_awaited()
    assert not app.gate._spoke_at
    assert not any(line.startswith("[学姐]") for line in persona_chat.get_memory().recent("g1"))
    assert app.gate._last_at.get("g1") == 1000


async def test_failed_attempt_keeps_attempt_cooldown_without_claiming_success(app):
    app.model.side_effect = RuntimeError("offline model failure")
    await app.bot._maybe_persona_interject(_message("今天真开心"), "今天真开心", "g1")
    await app.bot._maybe_persona_interject(_message("继续聊聊", mid="second"), "继续聊聊", "g1")
    app.model.assert_awaited_once()
    assert app.gate._last_at == {"g1": 1000}
    assert not app.gate._spoke_at


async def test_rate_rejected_attempt_does_not_mark_bot_spoke(app, monkeypatch):
    monkeypatch.setattr(config, "QA_RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(app.bot._qa_rate_limiter, "allow", lambda member: False)
    assert not await app.bot._maybe_persona_interject(_message("今天真开心"), "今天真开心", "g1")
    app.model.assert_not_awaited()
    app.api.post_group_message.assert_not_awaited()
    assert not app.gate._spoke_at
    assert not persona_chat.get_memory().recent("g1")


async def test_successful_prior_reply_blocks_interjection_during_quiet_window(app):
    assert await app.bot._safe_reply(_message("你好"), "你接着说。")
    assert not await app.bot._maybe_persona_interject(_message("今天真开心", mid="second"), "今天真开心", "g1")
    app.model.assert_not_awaited()
    app.api.post_group_message.assert_awaited_once()


def test_output_filter_does_not_reject_valid_business_fact_or_negated_arrival():
    assert private_chat.valid_persona_reply("比赛地点还没登记，等老师补充。")
    assert private_chat.valid_persona_reply("我没有到场。")


async def test_pure_interjection_prompt_has_no_group_business_facts(app):
    main.db.create_training_session("g1", {"title": "SECRET_COURSE", "weekday": "周六",
                                          "start_time": "19:00", "location": "SECRET_ROOM"})
    main.db.create_training_session("other", {"title": "OTHER_COURSE", "weekday": "周六",
                                             "start_time": "19:00", "location": "OTHER_ROOM"})
    await app.bot.on_group_at_message_create(_message("今天真开心"))
    system, prompt = app.model.await_args.args
    assert "SECRET_COURSE" not in prompt and "SECRET_ROOM" not in prompt
    assert "OTHER_COURSE" not in prompt and "OTHER_ROOM" not in prompt
    assert "本群已登记事实（" not in prompt
    assert "不代群友报告到场" in system and "闲聊不复读业务范围拒答" in system
    assert persona_chat.estimate_tokens(system) + persona_chat.estimate_tokens(prompt) <= 700


@pytest.mark.parametrize("text", ["我也到了", "到了机房啦", "刚到现场了", "你在干什么？"])
async def test_attendance_variants_and_unaddressed_questions_do_not_warm_or_trigger(app, text):
    await app.bot.on_group_at_message_create(_message(text))
    app.model.assert_not_awaited()
    app.api.post_group_message.assert_not_awaited()
    assert persona_chat.get_memory().recent("g1") == []


def test_self_location_requires_a_bot_or_second_person_subject():
    assert private_chat.local_persona_reply("你在哪里")
    assert private_chat.local_persona_reply("雄姐在哪")
    assert not private_chat.local_persona_reply("地点在哪里")
    assert not private_chat.local_persona_reply("在哪里")
