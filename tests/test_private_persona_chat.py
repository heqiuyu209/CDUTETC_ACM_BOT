"""真实 C2C 适配经私聊入口：称呼、聊天隔离及管理审批优先级。"""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from botpy.message import C2CMessage

from cdutetc import admin_session, config, database as db, llm, main, persona_chat, private_chat, qa_context
from cdutetc.bot.replies import _private_reply_context
from cdutetc.clients.qqbot import wrap_c2c_message


NOW = datetime(2026, 11, 20, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def app(tmp_path, monkeypatch, official_client):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "private_chat.sqlite3"))
    monkeypatch.setattr(db, "_delivery_now", lambda value=None: value or NOW)
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "ENV_TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "LLM_API_KEY", "offline-key")
    monkeypatch.setattr(config, "QA_SOCIAL_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "PERSONA_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "QA_RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(config, "QA_BLACKLIST", set())
    monkeypatch.setattr(config, "QA_WHITELIST_ENABLED", False)
    monkeypatch.setattr(config, "QA_WHITELIST", set())
    monkeypatch.setattr(config, "QA_SENSITIVE_GATE_ENABLED", True)
    monkeypatch.setattr(config, "QA_SENSITIVE_EXTRA_WORDS", set())
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_ENABLED", False)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_GROUPS", set())
    monkeypatch.setattr(config, "MEME_ENABLED", True)
    persona_file = tmp_path / "persona.txt"
    persona_file.write_text("你是雄姐，简短、自然地接话。状态：{status}", encoding="utf-8")
    monkeypatch.setattr(persona_chat, "PERSONA_FILE", persona_file)
    db.init_db()
    admin_session.reset_all()
    persona_chat.reset_engine()
    bot = main.ACMBot(official_client)
    model = AsyncMock(return_value="我在听，慢慢说。")
    monkeypatch.setattr(llm, "_chat_once", model)
    app = SimpleNamespace(bot=bot, api=official_client.api, model=model, monkeypatch=monkeypatch)
    yield app
    admin_session.reset_all()
    persona_chat.reset_engine()


def _message(app, text, user="member", message="private-inbound"):
    # C2CMessage 的作者只有 user_openid，与平台实际 SDK 数据结构一致。
    sdk = C2CMessage(app.api, "offline-event", {
        "id": message, "content": text, "author": {"user_openid": user},
    })
    return wrap_c2c_message(sdk)


async def _send(app, text, user="member", message="private-inbound"):
    before = app.api.post_c2c_message.await_count
    await app.bot.on_private_message_create(_message(app, text, user, message))
    params = [call.kwargs for call in app.api.post_c2c_message.await_args_list[before:]]
    for item in params:
        assert item["openid"] == user
        assert item["msg_id"] == message
        assert 1 <= item["msg_seq"] <= 4
        assert item["content"].strip()
    assert _private_reply_context.get() is None
    return params


def _no_business_reads(app):
    guard = Mock(side_effect=AssertionError("私聊社交不应读取数据库或群业务"))
    app.monkeypatch.setattr(db, "_connect", guard)
    app.monkeypatch.setattr(qa_context, "compact_business_facts", guard)
    app.monkeypatch.setattr(admin_session, "get_active_group", guard)
    app.monkeypatch.setattr(app.bot, "_send_meme", AsyncMock(side_effect=AssertionError("私聊不能发群表情包")))
    return guard


@pytest.mark.parametrize("user", ["admin", "teacher", "member"])
@pytest.mark.parametrize("text", ["雄姐", "雄姐在吗", "小雄"])
async def test_name_calls_all_roles_reply_without_model_or_business_reads(app, user, text):
    guard = _no_business_reads(app)
    replies = await _send(app, text, user)
    assert len(replies) == 1
    assert "在" in replies[0]["content"]
    app.model.assert_not_awaited()
    guard.assert_not_called()
    app.api.post_group_message.assert_not_awaited()


@pytest.mark.parametrize("user", ["admin", "teacher", "member"])
async def test_private_model_chat_is_bounded_without_database_or_group_data(app, user):
    guard = _no_business_reads(app)
    replies = await _send(app, "今天心情不错，想和你聊聊天", user)
    assert len(replies) == 1
    app.model.assert_awaited_once()
    system, prompt = app.model.await_args.args
    assert persona_chat.estimate_tokens(system) + persona_chat.estimate_tokens(prompt) <= 700
    assert "今天心情不错" in prompt
    assert "本群已登记事实（" not in prompt
    assert "你记得这个群友" not in prompt
    guard.assert_not_called()
    app.api.post_group_message.assert_not_awaited()
    app.bot._send_meme.assert_not_awaited()


async def test_admin_workspace_does_not_supply_group_facts_to_private_chat(app):
    db.create_training_session("g1", {"title": "绝不能泄漏的课程", "weekday": "周五",
                                       "start_time": "19:00", "end_time": "21:00", "location": "SECRET_ROOM"})
    db.add_memory_fact("g1", "admin", "绝不能泄漏的标签")
    admin_session.set_active_group("admin", "g1")
    guard = _no_business_reads(app)
    assert await _send(app, "陪我聊两句吧", "admin")
    system, prompt = app.model.await_args.args
    assert "绝不能泄漏" not in system + prompt
    assert "SECRET_ROOM" not in system + prompt
    guard.assert_not_called()


async def test_private_context_isolated_between_users_and_group_chat(app):
    persona_chat.get_memory().observe("g1", "群友", "GROUP_HISTORY_SECRET")
    assert await _send(app, "我喜欢草莓奶茶", "alice", "alice-1")
    assert await _send(app, "我喜欢椰子饮料", "bob", "bob-1")
    bob_prompt = app.model.await_args.args[1]
    assert "草莓奶茶" not in bob_prompt and "GROUP_HISTORY_SECRET" not in bob_prompt
    assert await _send(app, "接着陪我聊聊吧", "alice", "alice-2")
    alice_prompt = app.model.await_args.args[1]
    assert "草莓奶茶" in alice_prompt
    assert "椰子饮料" not in alice_prompt and "GROUP_HISTORY_SECRET" not in alice_prompt
    assert await _send(app, "接着陪我聊聊吧", "bob", "bob-2")
    bob_prompt = app.model.await_args.args[1]
    assert "椰子饮料" in bob_prompt and "草莓奶茶" not in bob_prompt
    app.api.post_group_message.assert_not_awaited()


@pytest.mark.parametrize("mode", ["no_key", "persona_disabled", "exception", "empty", "meme_only"])
async def test_private_chat_always_has_local_text_fallback(app, mode):
    if mode == "no_key":
        app.monkeypatch.setattr(config, "LLM_API_KEY", "")
    elif mode == "persona_disabled":
        app.monkeypatch.setattr(config, "PERSONA_CHAT_ENABLED", False)
    elif mode == "exception":
        app.model.side_effect = RuntimeError("offline model failed")
    elif mode == "empty":
        app.model.return_value = ""
    elif mode == "meme_only":
        app.model.return_value = "[MEME:ac]"
    replies = await _send(app, "今天心情不错，想和你聊聊天")
    assert len(replies) == 1
    assert "MEME" not in replies[0]["content"]
    if mode in {"no_key", "persona_disabled"}:
        app.model.assert_not_awaited()
    app.api.post_group_message.assert_not_awaited()


@pytest.mark.parametrize("restriction", ["social_disabled", "blacklist", "whitelist", "sensitive", "rate_limit"])
async def test_private_restrictions_do_not_call_model(app, restriction):
    text = "今天心情不错，想和你聊聊天"
    if restriction == "social_disabled":
        app.monkeypatch.setattr(config, "QA_SOCIAL_CHAT_ENABLED", False)
    elif restriction == "blacklist":
        app.monkeypatch.setattr(config, "QA_BLACKLIST", {"member"})
    elif restriction == "whitelist":
        app.monkeypatch.setattr(config, "QA_WHITELIST_ENABLED", True)
        app.monkeypatch.setattr(config, "QA_WHITELIST", {"someone-else"})
    elif restriction == "sensitive":
        app.monkeypatch.setattr(config, "QA_SENSITIVE_EXTRA_WORDS", {"测试拒绝词"})
        text += "测试拒绝词"
    else:
        app.monkeypatch.setattr(config, "QA_RATE_LIMIT_ENABLED", True)
        app.monkeypatch.setattr(app.bot._qa_rate_limiter, "allow", Mock(return_value=False))
    await _send(app, text)
    app.model.assert_not_awaited()
    app.api.post_group_message.assert_not_awaited()
    if restriction in {"social_disabled", "blacklist"}:
        app.api.post_c2c_message.assert_not_awaited()


async def test_blocked_private_message_is_not_saved_into_chat_context(app):
    app.monkeypatch.setattr(config, "QA_BLACKLIST", {"alice"})
    await _send(app, "我喜欢秘密草莓", "alice", "blocked")
    app.monkeypatch.setattr(config, "QA_BLACKLIST", set())
    assert await _send(app, "现在陪我聊两句", "alice", "allowed")
    assert "秘密草莓" not in app.model.await_args.args[1]


@pytest.mark.parametrize("user", ["admin", "teacher", "member"])
async def test_slash_commands_are_not_consumed_by_private_social_route(app, user):
    replies = await _send(app, "/帮助", user)
    assert replies
    if user != "admin":
        assert "超管" in replies[0]["content"] or "超级管理员" in replies[0]["content"]
    app.model.assert_not_awaited()
    app.api.post_group_message.assert_not_awaited()


@pytest.mark.parametrize("user", ["teacher", "admin"])
async def test_private_faq_confirmation_keeps_real_database_action(app, user):
    draft = db.add_faq_pending("g1", "请假如何报备", "联系负责老师", source=db.teacher_source("teacher"))
    replies = await _send(app, f"确认 {draft['faq_id']}", user)
    assert replies and "已确认" in replies[0]["content"]
    assert db.get_faq("g1", draft["faq_id"])["status"] == db.FAQ_CONFIRMED
    app.model.assert_not_awaited()


@pytest.mark.parametrize("action", ["拒绝", "拒绝 1", "驳回 1", "reject 1"])
async def test_publication_rejection_is_not_reclassified_as_chat(app, action):
    pid = db.create_pending_publication({"name": "待审比赛", "official_time": "2026-11-21 19:00"},
                                       "g1", "", "", "teacher")
    assert pid == 1
    replies = await _send(app, action, "admin")
    assert db.get_pending_publication(pid)["status"] == "rejected"
    assert replies and "拒绝" in replies[0]["content"]
    app.model.assert_not_awaited()


@pytest.mark.parametrize("action", ["同意", "同意 1", "通过", "批准 1", "approve 1", "ok", "y"])
async def test_publication_approval_is_not_reclassified_as_chat(app, action):
    pid = db.create_pending_publication({"name": "待审比赛", "official_time": "2026-11-21 19:00"},
                                       "g1", "", "", "teacher")
    assert pid == 1
    replies = await _send(app, action, "admin")
    assert db.get_pending_publication(pid)["status"] == "approved"
    assert replies
    assert len(db.list_competitions()) == 1
    app.model.assert_not_awaited()


@pytest.mark.parametrize("text", ["帮我修改这条记录", "把这条清空", "请删除这条记录"])
async def test_natural_management_action_stays_out_of_chat_model(app, text):
    replies = await _send(app, text, "admin")
    assert replies and "明确命令" in replies[0]["content"]
    app.model.assert_not_awaited()


@pytest.mark.parametrize("text", ["雄姐，这周群里有什么比赛", "课程地点在哪里", "帮我开启群周赛"])
async def test_member_business_message_keeps_group_redirect(app, text):
    db.set_group_teachers("g1", ["teacher"], operator="admin")
    replies = await _send(app, text, "member")
    assert replies
    assert "群" in replies[0]["content"] and "超管" in replies[0]["content"]
    assert "teacher" not in replies[0]["content"]
    app.model.assert_not_awaited()
    app.api.post_group_message.assert_not_awaited()


async def test_concurrent_private_users_keep_current_reply_ids(app):
    await asyncio.gather(
        app.bot.on_private_message_create(_message(app, "今天心情不错", "alice", "alice-current")),
        app.bot.on_private_message_create(_message(app, "想和你聊两句", "bob", "bob-current")),
    )
    assert app.api.post_c2c_message.await_count == 2
    assert {(call.kwargs["openid"], call.kwargs["msg_id"])
            for call in app.api.post_c2c_message.await_args_list} == {
                ("alice", "alice-current"), ("bob", "bob-current"),
            }
    assert _private_reply_context.get() is None


async def test_repeated_private_messages_never_borrow_previous_reply_id(app):
    assert await _send(app, "雄姐", "member", "first-private")
    assert await _send(app, "雄姐在吗", "member", "second-private")
    assert [call.kwargs["msg_id"] for call in app.api.post_c2c_message.await_args_list] == [
        "first-private", "second-private",
    ]
    assert all(call.kwargs["msg_seq"] == 1 for call in app.api.post_c2c_message.await_args_list)
    app.model.assert_not_awaited()


@pytest.mark.parametrize("text", ["雄姐", "雄姐在吗", "学姐", "你好"])
def test_name_calls_are_chat_but_mixed_business_is_not(text):
    assert private_chat.is_chat_message(text)
    assert not private_chat.is_chat_message(text + "，帮我关闭群周赛")
