"""人设聊天「@ 无反应」改造回归：身份单一事实源、动态称呼、本地兜底。

覆盖方案三个核心点：
  - bot_names/bot_name_regex 从 persona.txt @bot-name/@bot-alias 热读，缺省回退；
  - 喊名本地回复只认新识别名（雄姐/小雄），旧名「学姐」不再触发本地回复；
  - local_generic_reply 只在非业务/非决策/非敏感的纯闲聊下兜底一句，且注入主称呼。
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cdutetc import config, database as db, llm, main, persona_chat, private_chat
from cdutetc.clients.qqbot import wrap_group_at_message, wrap_group_message


@pytest.fixture
def identity(tmp_path, monkeypatch):
    """人设文件可写、身份缓存隔离：每个用例自己决定 @bot-name/@bot-alias。"""
    path = tmp_path / "persona.txt"
    monkeypatch.setattr(persona_chat, "PERSONA_FILE", path)
    persona_chat.reset_persona_cache()
    yield path
    persona_chat.reset_persona_cache()


# ---------------- 身份单一事实源 ----------------

def test_identity_parses_name_and_alias(identity):
    identity.write_text(
        "# @bot-name: 雄姐\n# @bot-alias: 小雄\n你是助教。状态：{status}",
        encoding="utf-8",
    )
    assert persona_chat.bot_names() == ["雄姐", "小雄"]
    assert persona_chat.bot_names()[0] == "雄姐"


def test_identity_falls_back_when_no_metadata(identity):
    identity.write_text("你是雄姐，简短接话。状态：{status}", encoding="utf-8")
    assert list(persona_chat.bot_names()) == list(persona_chat.DEFAULT_BOT_NAMES)
    assert persona_chat.bot_names()[0] == "雄姐"


def test_identity_falls_back_when_file_missing(identity):
    assert not identity.exists()
    assert list(persona_chat.bot_names()) == list(persona_chat.DEFAULT_BOT_NAMES)


def test_identity_hot_reload_after_file_change(identity):
    identity.write_text("# @bot-name: 学姐\n", encoding="utf-8")
    assert persona_chat.bot_names()[0] == "学姐"
    identity.write_text("# @bot-name: 雄姐\n# @bot-alias: 小雄\n", encoding="utf-8")
    assert persona_chat.bot_names() == ["雄姐", "小雄"]


def test_bot_name_regex_longest_first(identity):
    identity.write_text(
        "# @bot-name: 雄姐\n# @bot-alias: 小雄, 雄姐姐\n", encoding="utf-8"
    )
    regex = persona_chat.bot_name_regex()
    parts = regex.strip("(?:)").split("|")
    assert parts == ["雄姐姐", "雄姐", "小雄"]


# ---------------- 本地喊名回复：只认新识别名 ----------------

@pytest.mark.parametrize("text", ["雄姐", "雄姐在吗", "小雄", "小雄你好"])
def test_call_reply_uses_new_names(identity, text):
    identity.write_text("# @bot-name: 雄姐\n# @bot-alias: 小雄\n", encoding="utf-8")
    reply = private_chat.local_call_reply(text)
    assert reply == "在呢，雄姐听着～"


def test_call_reply_no_longer_matches_old_alias(identity):
    identity.write_text("# @bot-name: 雄姐\n# @bot-alias: 小雄\n", encoding="utf-8")
    assert private_chat.local_call_reply("学姐在吗") == ""


def test_renamed_primary_flows_into_call_reply(identity):
    """改人设文件即改称呼：文案与识别同步，无需改代码。"""
    identity.write_text("# @bot-name: 娜姐\n# @bot-alias: 小娜\n", encoding="utf-8")
    assert private_chat.local_call_reply("娜姐在吗") == "在呢，娜姐听着～"
    assert private_chat.local_call_reply("小娜你好") == "在呢，娜姐听着～"
    assert private_chat.local_generic_reply("随便聊聊") == (
        "在呢，我是娜姐～有空聊聊算法，别的回头再说。"
    )


# ---------------- 零 Token 通用兜底：过滤 + 称呼注入 ----------------

def test_generic_reply_only_for_plain_chat(identity):
    identity.write_text("# @bot-name: 雄姐\n# @bot-alias: 小雄\n", encoding="utf-8")
    assert "雄姐" in private_chat.local_generic_reply("今天真开心")
    # 命令、业务、决策、空文本一律不抢答
    assert private_chat.local_generic_reply("/帮助") == ""
    assert private_chat.local_generic_reply("") == ""
    assert private_chat.local_generic_reply("明天比赛报名开始了吗") == ""
    assert private_chat.local_generic_reply("同意 全部") == ""


# ---------------- @ 必回一句：模型/开关不可用时本地兜底 ----------------

@pytest.fixture
def group_app(tmp_path, monkeypatch, official_client):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "group_fallback.sqlite3"))
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "group_fallback.sqlite3"))
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1"})
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", {"g1"})
    monkeypatch.setattr(config, "GROUP_REQUIRE_AT", False)
    monkeypatch.setattr(config, "NL_INTENT_ENABLED", False)
    monkeypatch.setattr(config, "TEACHER_AUTO_INTENT_ENABLED", False)
    monkeypatch.setattr(config, "QA_SOCIAL_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "PERSONA_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "QA_SENSITIVE_GATE_ENABLED", True)
    monkeypatch.setattr(config, "QA_SENSITIVE_EXTRA_WORDS", set())
    monkeypatch.setattr(config, "NOTICE_UNAUTHORIZED_GROUP", False)
    monkeypatch.setattr(config, "MEME_ENABLED", False)
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    monkeypatch.setattr(config, "QA_SOCIAL_CHAT_USE_LLM", True)
    identity_file = tmp_path / "persona.txt"
    identity_file.write_text("# @bot-name: 雄姐\n# @bot-alias: 小雄\n", encoding="utf-8")
    monkeypatch.setattr(persona_chat, "PERSONA_FILE", identity_file)
    db.init_db()
    persona_chat.reset_engine()
    bot = main.ACMBot(official_client)
    yield SimpleNamespace(bot=bot, client=official_client, api=official_client.api)
    persona_chat.reset_engine()


def _group_at(app, text="今天真开心"):
    from botpy.message import GroupMessage
    raw = {
        "id": "fallback-msg", "group_openid": "g1", "content": "<@bot-openid> " + text,
        "author": {"member_openid": "member"}, "mentions": [],
    }
    return wrap_group_at_message(GroupMessage(app.api, "at-event", raw))


async def test_at_chat_replies_locally_when_no_model_key(group_app):
    message = _group_at(group_app, "今天真开心")
    await group_app.client._receive_group_message(message)
    sent = [c.kwargs["content"] for c in group_app.api.post_group_message.await_args_list]
    assert any("雄姐" in text for text in sent)
