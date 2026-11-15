"""自动收图与场景化发图在 main 层的接缝测试。

覆盖三件事：按群开关决定是否投递后台收图、OneBot 协议下用本机文件发图、
模型只给 [MEME:分类] 时只发图不发空文本。全程不联网、不调模型。
"""

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from cdutetc import config, database, main, memes, persona_chat
from cdutetc.clients.onebot import OneBotMessage


@pytest.fixture(autouse=True)
def memory_db(monkeypatch):
    """内存库 + 干净的单例引擎，避免用例之间串冷却与防重队列。"""
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    database.init_db()
    memes.reset_engine()
    persona_chat.reset_engine()
    yield conn
    conn.close()
    memes.reset_engine()
    persona_chat.reset_engine()


def _message(content="", group_id="g1", member_id="m1", message_id=1):
    return OneBotMessage({
        "post_type": "message", "message_type": "group", "message_id": message_id,
        "raw_message": content, "group_id": group_id, "user_id": member_id,
        "self_id": 30001,
    })


@pytest.fixture
def bot(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1"})
    monkeypatch.setattr(config, "GROUP_REQUIRE_AT", True)
    monkeypatch.setattr(config, "MEME_ENABLED", True)
    monkeypatch.setattr(config, "ECHO_ENABLED", False)
    monkeypatch.setattr(config, "PERSONA_CHAT_ENABLED", True)
    return main.ACMBot()


async def _drain(bot):
    tasks = list(bot._meme_tasks)
    if tasks:
        await asyncio.gather(*tasks)


async def test_collect_task_scheduled_when_group_enabled(bot, monkeypatch):
    """开关开启 + 群友发图：后台投递一次收图任务，且不阻塞当前消息。"""
    database.set_meme_collect_enabled("g1", True, "admin")
    calls = []

    async def fake_collect(message, *, group_id, text="", context=(), **kwargs):
        calls.append({"group_id": group_id, "text": text, "context": list(context)})
        return []

    monkeypatch.setattr(memes, "collect_from_message", fake_collect)
    await bot.on_group_at_message_create(_message("今天这发 AC 稳了 [CQ:image,file=a.png]"))
    await _drain(bot)
    assert calls == [{"group_id": "g1", "text": "今天这发 AC 稳了", "context": []}]


async def test_collect_skipped_when_group_disabled(bot, monkeypatch):
    """开关关闭：图片消息只进文本滑窗，绝不投递收图任务。"""
    calls = []

    async def fake_collect(*args, **kwargs):
        calls.append(kwargs.get("group_id"))
        return []

    monkeypatch.setattr(memes, "collect_from_message", fake_collect)
    await bot.on_group_at_message_create(_message("[CQ:image,file=a.png]"))
    await _drain(bot)
    assert calls == []
    assert memes.get_engine().recent_texts("g1") == []


async def test_send_meme_uses_local_file_on_onebot(bot, monkeypatch, tmp_path):
    """OneBot 协议直接发本机文件，并把已发图记进防重队列。"""
    root = tmp_path / "memes"
    (root / "ac").mkdir(parents=True)
    (root / "ac" / "绝杀_abc12345.png").write_bytes(b"fake")
    # 发图链路默认读模块级素材目录，测试里整体指向临时目录。
    monkeypatch.setattr(memes, "MEMES_DIR", root)
    engine = memes.get_engine()
    engine._root = root
    sent = []

    async def fake_image(group_id, image_path, *, reply_to=None, **_kwargs):
        sent.append((group_id, str(image_path), reply_to))
        return True

    client = SimpleNamespace(protocol="onebot", send_group_image_result=fake_image)
    onebot_bot = main.ACMBot(client)
    message = _message("绝杀！", message_id=77)
    assert await onebot_bot._send_meme(message, "g1", "ac", text="绝杀！") is True
    assert sent and sent[0][0] == "g1" and sent[0][1].endswith("绝杀_abc12345.png")
    assert engine.recent_sent("g1") == ["ac/绝杀_abc12345.png"]


async def test_persona_pure_meme_reply_sends_image_only(bot, monkeypatch):
    """模型只输出 [MEME:ac]：只发图，不发送空文本。"""
    replies = []
    images = []

    async def fake_safe_reply(message, content):
        replies.append(content)
        return True

    async def fake_send_meme(message, group_id, category, *, text="", proactive=False):
        images.append((group_id, category, text))
        return True

    async def fake_chat(system, user):
        return "[MEME:ac]"

    monkeypatch.setattr(bot, "_safe_reply", fake_safe_reply)
    monkeypatch.setattr(bot, "_send_meme", fake_send_meme)
    monkeypatch.setattr(persona_chat, "chat", fake_chat)
    handled = await bot._handle_persona_chat(_message("牛啊"), "牛啊", "g1", "m1")
    assert handled is True
    assert replies == []
    assert images == [("g1", "ac", "牛啊")]


async def test_persona_text_and_meme_both_sent(bot, monkeypatch):
    """图文同回：文本照发，表情包顺风车再发一张。"""
    replies = []
    images = []

    async def fake_safe_reply(message, content):
        replies.append(content)
        return True

    async def fake_send_meme(message, group_id, category, *, text="", proactive=False):
        images.append((group_id, category))
        return True

    async def fake_chat(system, user):
        return "过了过了 [MEME:ac]"

    monkeypatch.setattr(bot, "_safe_reply", fake_safe_reply)
    monkeypatch.setattr(bot, "_send_meme", fake_send_meme)
    monkeypatch.setattr(persona_chat, "chat", fake_chat)
    assert await bot._handle_persona_chat(_message("牛啊"), "牛啊", "g1", "m1") is True
    assert replies == ["过了过了"]
    assert images == [("g1", "ac")]
