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
    """开关/key 齐备：纯文本喊名走 LLM 生成带人设回复。"""
    app.monkeypatch.setattr(config, "NAME_ADDRESS_ENABLED", True)
    await app.client._receive_group_message(_plain("雄姐"))
    params = app.api.post_group_message.await_args.kwargs
    assert "雄姐" in params["content"]
    assert "听着呢" in params["content"]  # LLM mock 返回值，而非固定句
    app.model.assert_awaited_once()


@pytest.mark.parametrize("text", ["雄姐你好", "小雄在吗"])
async def test_plain_alias_on_replies(app, text):
    """开关开启：主名/别名开头均触发 LLM 人设回复。"""
    app.monkeypatch.setattr(config, "NAME_ADDRESS_ENABLED", True)
    await app.client._receive_group_message(_plain(text))
    assert app.api.post_group_message.await_count == 1
    app.model.assert_awaited_once()


async def test_plain_name_no_key_falls_back_to_fixed_line(app):
    """无 LLM API Key：一字不改回退本地固定句，不调用模型。"""
    app.monkeypatch.setattr(config, "NAME_ADDRESS_ENABLED", True)
    app.monkeypatch.setattr(config, "LLM_API_KEY", "")
    await app.client._receive_group_message(_plain("雄姐"))
    params = app.api.post_group_message.await_args.kwargs
    assert "雄姐" in params["content"]
    assert "听着" in params["content"]
    app.model.assert_not_awaited()


async def test_plain_name_llm_exception_falls_back_to_fixed_line(app):
    """LLM 抛异常：回退本地固定句，链路不中断。"""
    app.monkeypatch.setattr(config, "NAME_ADDRESS_ENABLED", True)
    app.model.side_effect = RuntimeError("boom")
    await app.client._receive_group_message(_plain("雄姐"))
    params = app.api.post_group_message.await_args.kwargs
    assert "雄姐" in params["content"]
    app.model.assert_awaited_once()


async def test_plain_name_llm_empty_output_falls_back_to_fixed_line(app):
    """LLM 输出被过滤为空：回退本地固定句。"""
    app.monkeypatch.setattr(config, "NAME_ADDRESS_ENABLED", True)
    app.model.return_value = ""
    await app.client._receive_group_message(_plain("雄姐"))
    params = app.api.post_group_message.await_args.kwargs
    assert "雄姐" in params["content"]
    app.model.assert_awaited_once()


async def test_plain_name_uses_balanced_token_params(app, monkeypatch):
    """均衡模式：预算 600 / 上下文 <=4 / max_tokens 100，人设 system 不裁剪。"""
    import cdutetc.persona_chat as pc
    app.monkeypatch.setattr(config, "NAME_ADDRESS_ENABLED", True)
    captured = {}
    real_build, real_chat = pc.build_conversation, pc.chat

    def spy_build(**kwargs):
        captured["budget"] = kwargs.get("budget")
        captured["context"] = list(kwargs.get("context") or [])
        return real_build(**kwargs)

    async def spy_chat(system, user, **kwargs):
        captured["max_tokens"] = kwargs.get("max_tokens")
        captured["system"] = system
        return "雄姐听着呢。"

    monkeypatch.setattr(pc, "build_conversation", spy_build)
    monkeypatch.setattr(pc, "chat", spy_chat)
    await app.client._receive_group_message(_plain("雄姐"))
    assert captured["budget"] == 600
    assert len(captured["context"]) <= 4
    assert captured["max_tokens"] == 100
    assert captured["system"]


@pytest.mark.parametrize("enabled", [False, True])
async def test_real_at_replies_regardless_of_switch(app, enabled):
    """真实 @ 无论开关都回复；开关开时先走 LLM，关时回固定句。"""
    app.monkeypatch.setattr(config, "NAME_ADDRESS_ENABLED", enabled)
    await app.client._receive_group_message(_at(app))
    params = app.api.post_group_message.await_args.kwargs
    assert "雄姐" in params["content"]
    if enabled:
        app.model.assert_awaited_once()  # 真 @ 命中本地固定句也先走 LLM
    else:
        app.model.assert_not_awaited()   # 开关关：回退固定句


@pytest.mark.parametrize("text", ["雄姐", "雄姐？", "雄姐你好"])
async def test_real_at_name_goes_llm(app, text):
    """真 @ 命中本地固定短句（含问号/别名）时先走均衡 LLM，回复取模型生成。"""
    app.monkeypatch.setattr(config, "NAME_ADDRESS_ENABLED", True)
    await app.client._receive_group_message(_at(app, text=text))
    params = app.api.post_group_message.await_args.kwargs
    assert "听着呢" in params["content"]  # LLM mock 返回值，而非固定句「在呢，…听着～」
    app.model.assert_awaited_once()


async def test_real_at_name_no_key_falls_back_to_fixed_line(app):
    """真 @ 喊名但无 LLM Key：一字不改回退本地固定句，不调用模型。"""
    app.monkeypatch.setattr(config, "NAME_ADDRESS_ENABLED", True)
    app.monkeypatch.setattr(config, "LLM_API_KEY", "")
    await app.client._receive_group_message(_at(app))
    params = app.api.post_group_message.await_args.kwargs
    assert "在呢" in params["content"] and "雄姐" in params["content"]  # 固定句
    app.model.assert_not_awaited()


async def test_plain_name_not_at_prefix_stays_silent_even_when_on(app):
    """识别名不在开头时，即使开关开启也不触发。"""
    app.monkeypatch.setattr(config, "NAME_ADDRESS_ENABLED", True)
    await app.client._receive_group_message(_plain("你好呀雄姐"))
    app.api.post_group_message.assert_not_awaited()
    app.model.assert_not_awaited()
