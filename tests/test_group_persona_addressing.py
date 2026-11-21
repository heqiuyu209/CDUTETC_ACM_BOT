"""官方真假 @、双事件顺序和群内称呼经真实适配与群消息入口验证。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from botpy.message import GroupMessage

from cdutetc import config, database as db, llm, main, persona_chat
from cdutetc.clients.qqbot import wrap_group_at_message, wrap_group_message
from cdutetc.utils import is_addressed_to_bot, strip_at


@pytest.fixture
def app(tmp_path, monkeypatch, official_client):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "group_alias.sqlite3"))
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1"})
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", {"g1"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "ENV_TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "GROUP_REQUIRE_AT", True)
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    monkeypatch.setattr(config, "NL_INTENT_ENABLED", False)
    monkeypatch.setattr(config, "TEACHER_AUTO_INTENT_ENABLED", False)
    monkeypatch.setattr(config, "QA_SOCIAL_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "PERSONA_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "QA_SOCIAL_CHAT_USE_LLM", False)
    monkeypatch.setattr(config, "QA_MEMBER_CONTEXT_ANSWER", False)
    monkeypatch.setattr(config, "QA_BLACKLIST", set())
    monkeypatch.setattr(config, "QA_WHITELIST_ENABLED", False)
    monkeypatch.setattr(config, "QA_WHITELIST", set())
    monkeypatch.setattr(config, "QA_RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(config, "QA_SENSITIVE_GATE_ENABLED", True)
    monkeypatch.setattr(config, "QA_SENSITIVE_EXTRA_WORDS", set())
    monkeypatch.setattr(config, "NOTICE_UNAUTHORIZED_GROUP", False)
    monkeypatch.setattr(config, "LLM_API_KEY", "offline-key")
    monkeypatch.setattr(config, "MEME_ENABLED", False)
    persona_file = tmp_path / "persona.txt"
    persona_file.write_text("你是雄姐，简短接话。状态：{status}", encoding="utf-8")
    monkeypatch.setattr(persona_chat, "PERSONA_FILE", persona_file)
    db.init_db()
    persona_chat.reset_engine()
    model = AsyncMock(return_value="雄姐听着呢。")
    monkeypatch.setattr(llm, "_chat_once", model)
    bot = main.ACMBot(official_client)
    monkeypatch.setattr(bot, "_schedule_meme_collect", Mock())
    yield SimpleNamespace(bot=bot, client=official_client, api=official_client.api,
                          model=model, monkeypatch=monkeypatch)
    persona_chat.reset_engine()


def _raw(text="雄姐", *, group="g1", user="member", message="current-message", mentions=None):
    return {
        "id": message, "group_openid": group, "content": "<@bot-openid> " + text,
        "author": {"member_openid": user},
        "mentions": [] if mentions is None else mentions,
    }


def _at_message(app, text="雄姐", **kwargs):
    # botpy 1.2.1 会丢掉 mentions.is_you，GROUP_AT 事件类型本身仍证明定向。
    sdk = GroupMessage(app.api, "real-group-at-event", _raw(text, **kwargs))
    return wrap_group_at_message(sdk)


def _assert_current_reply(app, message="current-message"):
    app.api.post_group_message.assert_awaited_once()
    params = app.api.post_group_message.await_args.kwargs
    assert params["group_openid"] == "g1"
    assert params["msg_id"] == message
    assert params["msg_seq"] == 1
    assert params["content"].strip()
    assert "问题编号" not in params["content"]
    app.api.post_c2c_message.assert_not_awaited()
    assert db.list_unanswered("g1") == []
    return params["content"]


@pytest.mark.parametrize("text", ["雄姐", "雄姐你好", "雄姐是谁", "雄姐在吗", "学姐"])
@pytest.mark.parametrize("user", ["member", "teacher", "admin"])
async def test_real_group_at_alias_replies_locally_without_business_question(app, text, user):
    message = _at_message(app, text, user=user)
    assert is_addressed_to_bot(message)
    assert strip_at(message.content) == text
    await app.client._receive_group_message(message)
    _assert_current_reply(app)
    app.model.assert_not_awaited()


async def test_raw_full_group_true_platform_mention_can_trigger_alias_reply(app):
    message = wrap_group_message(_raw(mentions=[{"id": "bot-openid", "is_you": True}]))
    assert is_addressed_to_bot(message)
    await app.client._receive_group_message(message)
    _assert_current_reply(app)
    app.model.assert_not_awaited()


@pytest.mark.parametrize("mentions", [
    [], [{"id": "bot-openid", "is_you": False}], [{"id": "bot-openid", "is_you": "true"}],
    [{"id": "other-bot", "is_you": False}],
])
async def test_textual_or_other_bot_mention_cannot_fake_platform_addressing(app, mentions):
    message = wrap_group_message(_raw(mentions=mentions))
    assert not is_addressed_to_bot(message)
    await app.client._receive_group_message(message)
    app.api.post_group_message.assert_not_awaited()
    app.model.assert_not_awaited()
    assert db.list_unanswered("g1") == []


@pytest.mark.parametrize("first", ["full_unaddressed", "real_at"])
async def test_real_at_is_not_lost_after_unaddressed_full_event_with_same_message_id(app, first):
    raw = _raw(message="dual-event-id")
    full = wrap_group_message(raw)
    real_at = wrap_group_at_message(GroupMessage(app.api, "at-event", raw))
    assert not is_addressed_to_bot(full)
    assert is_addressed_to_bot(real_at)
    messages = [full, real_at] if first == "full_unaddressed" else [real_at, full]
    for message in messages:
        await app.client._receive_group_message(message)
    _assert_current_reply(app, "dual-event-id")
    app.model.assert_not_awaited()


@pytest.mark.parametrize("first", ["full", "at"])
async def test_two_addressed_events_for_one_message_reply_once(app, first):
    raw = _raw(message="both-addressed", mentions=[{"id": "bot-openid", "is_you": True}])
    full = wrap_group_message(raw)
    real_at = wrap_group_at_message(GroupMessage(app.api, "at-event", raw))
    messages = [full, real_at] if first == "full" else [real_at, full]
    for message in messages:
        await app.client._receive_group_message(message)
    _assert_current_reply(app, "both-addressed")
    app.model.assert_not_awaited()


async def test_only_platform_at_with_no_body_replies_locally(app):
    incoming = _at_message(app, "")
    assert strip_at(incoming.content) == ""
    await app.client._receive_group_message(incoming)
    _assert_current_reply(app)
    app.model.assert_not_awaited()


async def test_platform_at_with_only_attachment_stays_in_attachment_flow(app):
    raw = _raw("")
    raw["attachments"] = [{"url": "https://example.invalid/image.png", "content_type": "image/png"}]
    incoming = wrap_group_at_message(GroupMessage(app.api, "at-image", raw))
    await app.client._receive_group_message(incoming)
    app.bot._schedule_meme_collect.assert_called_once()
    app.api.post_group_message.assert_not_awaited()
    app.model.assert_not_awaited()


async def test_fake_text_at_without_body_does_not_get_local_call_reply(app):
    incoming = wrap_group_message(_raw(""))
    assert not is_addressed_to_bot(incoming)
    await app.client._receive_group_message(incoming)
    app.api.post_group_message.assert_not_awaited()
    app.model.assert_not_awaited()


@pytest.mark.parametrize("restriction", ["unauthorized_group", "qa_disabled", "social_disabled",
                                          "blacklist", "whitelist", "sensitive", "rate_limit"])
async def test_alias_obeys_existing_group_and_qa_gates(app, restriction):
    group = "g1"
    if restriction == "unauthorized_group":
        group = "other-group"
    elif restriction == "qa_disabled":
        db.set_group_switch("g1", "qa_enabled", False, operator="admin")
    elif restriction == "social_disabled":
        app.monkeypatch.setattr(config, "QA_SOCIAL_CHAT_ENABLED", False)
    elif restriction == "blacklist":
        app.monkeypatch.setattr(config, "QA_BLACKLIST", {"member"})
    elif restriction == "whitelist":
        app.monkeypatch.setattr(config, "QA_WHITELIST_ENABLED", True)
        app.monkeypatch.setattr(config, "QA_WHITELIST", {"other-member"})
    elif restriction == "sensitive":
        app.monkeypatch.setattr(config, "QA_SENSITIVE_EXTRA_WORDS", {"雄姐"})
    else:
        app.monkeypatch.setattr(config, "QA_RATE_LIMIT_ENABLED", True)
        app.monkeypatch.setattr(app.bot._qa_rate_limiter, "allow", Mock(return_value=False))
    await app.client._receive_group_message(_at_message(app, group=group))
    app.model.assert_not_awaited()
    assert db.list_unanswered("g1") == []
    if restriction in {"unauthorized_group", "qa_disabled", "social_disabled", "blacklist"}:
        app.api.post_group_message.assert_not_awaited()


async def test_alias_is_not_an_implicit_at_in_full_group_chat(app):
    message = wrap_group_message({
        "id": "no-at", "group_openid": "g1", "content": "雄姐你好",
        "author": {"member_openid": "member"}, "mentions": [],
    })
    assert not is_addressed_to_bot(message)
    await app.client._receive_group_message(message)
    app.api.post_group_message.assert_not_awaited()
    app.model.assert_not_awaited()


async def test_alias_with_explicit_command_keeps_command_path(app):
    await app.client._receive_group_message(_at_message(app, "/帮助"))
    reply = _assert_current_reply(app)
    assert "帮助" in reply or "/" in reply
    app.model.assert_not_awaited()
