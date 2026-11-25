"""正文喊名触发人设聊天（NAME_ADDRESS_ENABLED）回归。

纯文本正文（mentions=[]，无平台 @）以识别名开头时：
开关开 → 走 local_persona_reply 本地回复；开关关 → 保持静默。
真实 @ 消息不受开关影响。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from botpy.message import GroupMessage

from cdutetc import config, database as db, llm, main, persona_chat
from cdutetc.clients.qqbot import wrap_group_at_message, wrap_group_message


@pytest.fixture
def app(tmp_path, monkeypatch, official_client):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "name_address.sqlite3"))
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
    monkeypatch.setattr(config, "NAME_ADDRESS_ENABLED", False)
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


def _plain(text, *, message="plain-msg", group="g1", user="member"):
    """无平台 @ 的普通群消息（mentions 为空）。"""
    return wrap_group_message({
        "id": message, "group_openid": group, "content": text,
        "author": {"member_openid": user}, "mentions": [],
    })


def _at(app, text="雄姐", *, message="at-msg"):
    """真实 @ 事件：GROUP_AT 事件类型 + mentions.is_you。"""
    raw = {
        "id": message, "group_openid": "g1", "content": "<@bot-openid> " + text,
        "author": {"member_openid": "member"},
        "mentions": [{"id": "bot-openid", "is_you": True}],
    }
    return wrap_group_at_message(GroupMessage(app.api, "at-event", raw))


async def test_plain_name_off_stays_silent(app):
    """开关关闭：纯文本喊名不回复。"""
    await app.client._receive_group_message(_plain("雄姐"))
    app.api.post_group_message.assert_not_awaited()
    app.model.assert_not_awaited()


async def test_plain_name_on_replies_locally(app):
    """开关开启：纯文本喊名本地回复，且不调用模型。"""
    app.monkeypatch.setattr(config, "NAME_ADDRESS_ENABLED", True)
    await app.client._receive_group_message(_plain("雄姐"))
    params = app.api.post_group_message.await_args.kwargs
    assert "雄姐" in params["content"]
    app.model.assert_not_awaited()


@pytest.mark.parametrize("text", ["雄姐你好", "小雄在吗"])
async def test_plain_alias_on_replies(app, text):
    """开关开启：主名/别名开头均触发本地回复。"""
    app.monkeypatch.setattr(config, "NAME_ADDRESS_ENABLED", True)
    await app.client._receive_group_message(_plain(text))
    assert app.api.post_group_message.await_count == 1
    app.model.assert_not_awaited()


@pytest.mark.parametrize("enabled", [False, True])
async def test_real_at_replies_regardless_of_switch(app, enabled):
    """真实 @ 消息无论开关状态都正常回复。"""
    app.monkeypatch.setattr(config, "NAME_ADDRESS_ENABLED", enabled)
    await app.client._receive_group_message(_at(app))
    params = app.api.post_group_message.await_args.kwargs
    assert "雄姐" in params["content"]
    app.model.assert_not_awaited()


async def test_plain_name_not_at_prefix_stays_silent_even_when_on(app):
    """识别名不在开头时，即使开关开启也不触发。"""
    app.monkeypatch.setattr(config, "NAME_ADDRESS_ENABLED", True)
    await app.client._receive_group_message(_plain("你好呀雄姐"))
    app.api.post_group_message.assert_not_awaited()
    app.model.assert_not_awaited()
