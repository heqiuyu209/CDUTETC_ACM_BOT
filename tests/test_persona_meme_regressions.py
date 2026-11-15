"""聊天开关、过滤与双协议收发图的入口回归；仅模拟外部 I/O。"""
import asyncio
import io
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cdutetc import config, content_filter, database as db, main, memes, notifications, persona_chat
from cdutetc.clients.onebot import OneBotClient, OneBotMessage
from cdutetc.clients.qqbot import QQBotClient, wrap_group_message, wrap_group_at_message
from cdutetc.errors import DeliveryCode, DeliveryResult


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(db, "_connect", lambda: conn)
    db.init_db()
    values = {
        "ALLOWED_GROUPS": {"10001"}, "BOT_QQ": "30001",
        "GROUP_REQUIRE_AT": True, "TEACHER_AUTO_INTENT_ENABLED": False,
        "TEACHER_WHITELIST": set(), "NL_INTENT_ENABLED": False,
        "QA_RATE_LIMIT_ENABLED": False, "QA_BLACKLIST": set(),
        "QA_WHITELIST_ENABLED": False, "QA_WHITELIST": set(),
        "QA_SOCIAL_CHAT_ENABLED": True, "QA_SENSITIVE_GATE_ENABLED": True,
        "QA_SENSITIVE_EXTRA_WORDS": {"blocked-marker"},
        "PERSONA_CHAT_ENABLED": True, "ECHO_ENABLED": False,
        "MEME_ENABLED": False, "GROUP_ACTIVE_MESSAGES_ENABLED": True,
        "MEME_BASE_URL": "https://example.invalid/memes",
    }
    for key, value in values.items():
        monkeypatch.setattr(config, key, value)
    monkeypatch.setattr(memes, "MEMES_DIR", tmp_path)
    for category in memes.CATEGORIES:
        (tmp_path / category).mkdir()
        (tmp_path / category / "1.png").write_bytes(b"fixture")
    memes.reset_engine()
    persona_chat.reset_engine()
    yield
    conn.close()
    memes.reset_engine()
    persona_chat.reset_engine()


def message(text="在吗", *, at=True, member="u1", index=1):
    prefix = "[CQ:at,qq=30001] " if at else ""
    return OneBotMessage({"message_type": "group", "message_id": index,
                          "group_id": "10001", "user_id": member,
                          "raw_message": prefix + text})


def text_bot(monkeypatch, response="学姐来了"):
    bot = main.ACMBot()
    chat = AsyncMock(return_value=response)
    reply = AsyncMock(return_value=True)
    monkeypatch.setattr(persona_chat, "chat", chat)
    monkeypatch.setattr(bot, "_safe_reply", reply)
    return bot, chat, reply


@pytest.mark.parametrize("wrapper", [wrap_group_message, wrap_group_at_message])
@pytest.mark.parametrize("as_object", [False, True])
def test_official_wrapper_preserves_collectable_attachments(wrapper, as_object):
    event = {"id": "event-1", "group_openid": "10001", "content": "",
             "author": {"member_openid": "u1"},
             "attachments": [{"url": "https://example.invalid/a.png",
                              "filename": "a.png", "content_type": "image/png"}]}
    if as_object:
        event = SimpleNamespace(**event)
    wrapped = wrapper(event)
    assert memes.extract_image_refs(wrapped) == [
        {"url": "https://example.invalid/a.png", "file": "a.png"}]


async def test_scene_image_reaches_official_media_sender(monkeypatch):
    monkeypatch.setattr(config, "MEME_ENABLED", True)
    monkeypatch.setattr(config, "PERSONA_CHAT_ENABLED", False)
    monkeypatch.setattr(memes.get_engine(), "_rng", lambda: 1.0)
    client = QQBotClient("dummy", "dummy")
    client.upload_group_media = AsyncMock(return_value=("file-info", 1800))
    client.send_group_media_result = AsyncMock(
        return_value=DeliveryResult(True, DeliveryCode.SENT, "qqbot"))
    bot = main.ACMBot(client)
    for index in (1, 2):
        incoming = wrap_group_message({"id": str(index), "group_openid": "10001",
                                       "author": {"member_openid": "u1"},
                                       "content": "？"})
        await bot.on_group_at_message_create(incoming)
    assert client.send_group_media_result.await_count == 1


async def test_scene_image_obeys_active_messages_switch(monkeypatch):
    monkeypatch.setattr(config, "MEME_ENABLED", True)
    monkeypatch.setattr(config, "PERSONA_CHAT_ENABLED", False)
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", False)
    monkeypatch.setattr(memes.get_engine(), "_rng", lambda: 1.0)
    client = OneBotClient("ws://127.0.0.1:1")
    client.call_api = AsyncMock(return_value={"status": "ok", "retcode": 0,
                                             "data": {"message_id": 10}})
    bot = main.ACMBot(client)
    for index in (1, 2):
        await bot.on_group_at_message_create(message("？", at=False, index=index))
    client.call_api.assert_not_awaited()


async def test_addressed_chat_obeys_social_disabled(monkeypatch):
    monkeypatch.setattr(config, "QA_SOCIAL_CHAT_ENABLED", False)
    bot, chat, reply = text_bot(monkeypatch)
    await bot.on_group_at_message_create(message())
    chat.assert_not_awaited()
    reply.assert_not_awaited()


@pytest.mark.parametrize("disabled", ["group_qa", "social", "blacklist", "whitelist", "sensitive"])
async def test_interjection_obeys_existing_input_controls(monkeypatch, disabled):
    if disabled == "group_qa":
        db.set_group_switch("10001", "qa_enabled", False)
    elif disabled == "social":
        monkeypatch.setattr(config, "QA_SOCIAL_CHAT_ENABLED", False)
    elif disabled == "blacklist":
        monkeypatch.setattr(config, "QA_BLACKLIST", {"u1"})
    elif disabled == "whitelist":
        monkeypatch.setattr(config, "QA_WHITELIST_ENABLED", True)
        monkeypatch.setattr(config, "QA_WHITELIST", {"someone-else"})
    bot, chat, reply = text_bot(monkeypatch)
    monkeypatch.setattr(persona_chat.get_gate(), "_rng", lambda: 0.0)
    for index in range(persona_chat.INTERJECT_MIN_MESSAGES):
        text = "blocked-marker" if disabled == "sensitive" else f"闲聊{index}"
        await bot.on_group_at_message_create(message(text, at=False, index=index))
    chat.assert_not_awaited()
    reply.assert_not_awaited()
    assert persona_chat.get_memory().recent("10001") == []


async def test_persona_reply_passes_output_filter(monkeypatch):
    bot, chat, reply = text_bot(monkeypatch, response="blocked-marker")
    assert content_filter.sanitize_output("blocked-marker") == config.QA_OUTPUT_FILTERED_REPLY
    await bot.on_group_at_message_create(message())
    assert reply.await_args.args[1] == config.QA_OUTPUT_FILTERED_REPLY


async def test_failed_persona_delivery_is_not_reported_or_remembered_as_sent(monkeypatch):
    bot, chat, reply = text_bot(monkeypatch)
    reply.return_value = False
    handled = await bot._handle_persona_chat(message(), "在吗", "10001", "u1")
    assert (handled, persona_chat.get_memory().recent("10001")) == (
        False, ["[群友u1]: 在吗"])


def test_string_cq_image_url_preserves_signature():
    raw_url = "https://example.invalid/a.png?appid=1&amp;token=abc%2Fdef"
    wrapped = message(f"[CQ:image,file=a.png,url={raw_url}]", at=False)
    assert memes.extract_image_refs(wrapped)[0]["url"] == (
        "https://example.invalid/a.png?appid=1&token=abc%2Fdef")


async def test_control_official_passive_media_sends(monkeypatch):
    monkeypatch.setattr(config, "MEME_ENABLED", True)
    client = QQBotClient("dummy", "dummy")
    client.upload_group_media = AsyncMock(return_value=("file-info", 1800))
    client.send_group_media_result = AsyncMock(
        return_value=DeliveryResult(True, DeliveryCode.SENT, "qqbot"))
    bot = main.ACMBot(client)
    assert await bot._send_meme(message(), "10001", "question")
    client.send_group_media_result.assert_awaited_once()


async def test_control_group_qa_off_blocks_addressed_chat(monkeypatch):
    db.set_group_switch("10001", "qa_enabled", False)
    bot, chat, reply = text_bot(monkeypatch)
    await bot.on_group_at_message_create(message())
    chat.assert_not_awaited()
    reply.assert_not_awaited()


async def test_control_enabled_interjection_sends_once(monkeypatch):
    bot, chat, reply = text_bot(monkeypatch)
    monkeypatch.setattr(persona_chat.get_gate(), "_rng", lambda: 0.0)
    for index in range(persona_chat.INTERJECT_MIN_MESSAGES):
        await bot.on_group_at_message_create(message(f"闲聊{index}", at=False, index=index))
    chat.assert_awaited_once()
    reply.assert_awaited_once()


def test_control_segment_image_url_is_preserved():
    url = "https://example.invalid/a.png?appid=1&token=abc%2Fdef"
    wrapped = OneBotMessage({"message": [{"type": "image", "data": {
        "url": url, "file": "a.png"}}]})
    assert memes.extract_image_refs(wrapped) == [{"url": url, "file": "a.png"}]


async def test_interjection_rate_limit_only_counts_actual_llm_calls(monkeypatch):
    monkeypatch.setattr(config, "QA_RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(config, "QA_RATE_LIMIT_MAX_CALLS", 1)
    bot, chat, reply = text_bot(monkeypatch)
    monkeypatch.setattr(persona_chat.get_gate(), "_rng", lambda: 0.0)
    for index in range(persona_chat.INTERJECT_MIN_MESSAGES - 1):
        await bot.on_group_at_message_create(message(f"普通闲聊{index}", at=False, index=index))
    # 前十一条没有模型调用，不消耗真正的 @ 问答额度。
    await bot.on_group_at_message_create(message())
    chat.assert_awaited_once()
    reply.reset_mock()
    await bot.on_group_at_message_create(message("第十二句", at=False, index=12))
    chat.assert_awaited_once()
    reply.assert_not_awaited()


async def test_interjection_current_message_not_duplicated_in_prompt(monkeypatch):
    bot, chat, reply = text_bot(monkeypatch)
    monkeypatch.setattr(persona_chat.get_gate(), "_rng", lambda: 0.0)
    for index in range(persona_chat.INTERJECT_MIN_MESSAGES):
        await bot.on_group_at_message_create(message(f"唯一文本{index}", at=False, index=index))
    assert chat.await_args.args[1].count("唯一文本11") == 1


@pytest.mark.parametrize("text_ok,image_ok", [(False, False), (False, True), (True, False), (True, True)])
async def test_persona_delivery_result_matches_actual_text_or_image(monkeypatch, text_ok, image_ok):
    monkeypatch.setattr(config, "MEME_ENABLED", True)
    bot, chat, reply = text_bot(monkeypatch, response="接住 [MEME:ac]")
    reply.return_value = text_ok
    monkeypatch.setattr(bot, "_send_meme", AsyncMock(return_value=image_ok))
    assert await bot._handle_persona_chat(message(), "在吗", "10001", "u1") == (text_ok or image_ok)
    assert ("[学姐]: 接住" in persona_chat.get_memory().recent("10001")) == text_ok


async def test_output_filter_checks_full_reply_before_truncation_and_meme(monkeypatch):
    monkeypatch.setattr(config, "MEME_ENABLED", True)
    bot, chat, reply = text_bot(monkeypatch, response="好" * 100 + "blocked-marker [MEME:ac]")
    image = AsyncMock(return_value=True)
    monkeypatch.setattr(bot, "_send_meme", image)
    await bot.on_group_at_message_create(message())
    assert reply.await_args.args[1] == config.QA_OUTPUT_FILTERED_REPLY
    image.assert_not_awaited()


@pytest.mark.parametrize("wrapper", [wrap_group_message, wrap_group_at_message])
async def test_real_sdk_image_reaches_background_collection(monkeypatch, tmp_path, wrapper):
    sdk = pytest.importorskip("botpy.message")
    from PIL import Image

    image = io.BytesIO()
    Image.new("RGB", (200, 200), "blue").save(image, "PNG")
    fetch = AsyncMock(return_value=image.getvalue())
    monkeypatch.setattr(memes, "fetch_image_bytes", fetch)
    db.set_meme_collect_enabled("10001", True)
    event = sdk.GroupMessage(None, "evt", {
        "id": "image-event", "group_openid": "10001", "content": "",
        "author": {"member_openid": "u1"},
        "attachments": [{"url": "https://example.invalid/a.png", "filename": "a.png",
                         "content_type": "image/png"}],
    })
    bot = main.ACMBot()
    await bot.on_group_at_message_create(wrapper(event))
    await asyncio.gather(*list(bot._meme_tasks))
    fetch.assert_awaited_once_with("https://example.invalid/a.png")
    assert db.count_meme_hashes() == 1
    assert list(tmp_path.rglob("*.png"))  # 临时库中包含新收素材
    stored = db.get_meme_hash(memes.image_md5(image.getvalue()))
    assert (tmp_path / stored["file_path"]).read_bytes() == image.getvalue()


def test_non_image_official_attachment_is_ignored():
    wrapped = wrap_group_message({"attachments": [SimpleNamespace(
        url="https://example.invalid/a.gif", filename="a.gif", content_type="application/pdf")]})
    assert memes.extract_image_refs(wrapped) == []


@pytest.mark.parametrize("cq,expected", [
    ("https://example.invalid/?a=1&amp;b=2&#44;3", "https://example.invalid/?a=1&b=2,3"),
    ("https://example.invalid/?a=&#91;x&#93;", "https://example.invalid/?a=[x]"),
    ("https://example.invalid/?a=&amp;#44;", "https://example.invalid/?a=&#44;"),
])
def test_cq_parameter_entities_are_decoded_once(cq, expected):
    assert memes.extract_image_refs(message(f"[CQ:image,url={cq}]"))[0]["url"] == expected


def official_media_bot(monkeypatch):
    monkeypatch.setattr(config, "MEME_ENABLED", True)
    client = QQBotClient("dummy", "dummy")
    client.upload_group_media = AsyncMock(return_value=("file-info", 1800))
    api = SimpleNamespace(post_group_message=AsyncMock(return_value={"id": "accepted"}))
    client._client = SimpleNamespace(api=api)
    return main.ACMBot(client), client, api


async def test_official_active_media_uses_real_payload_and_passive_keeps_msg_id(monkeypatch):
    bot, client, api = official_media_bot(monkeypatch)
    assert await bot._send_meme(message(index=42), "10001", "question", proactive=True)
    payload = api.post_group_message.await_args.kwargs
    assert payload["msg_type"] == 7 and payload["media"] == {"file_info": "file-info"}
    assert "msg_id" not in payload
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", False)
    assert await bot._send_meme(message(index=43), "10001", "question")
    assert api.post_group_message.await_args.kwargs["msg_id"] == "43"
    assert not await bot._send_meme(message(index=44), "10001", "question", proactive=True)
    assert api.post_group_message.await_count == 2


async def test_official_active_media_recovers_once_after_403_cooldown(monkeypatch):
    bot, client, api = official_media_bot(monkeypatch)
    clock = SimpleNamespace(now=datetime(2026, 11, 15, 10, tzinfo=db.TZ))
    monkeypatch.setattr(db, "_delivery_now", lambda value=None: value or clock.now)
    monkeypatch.setattr(db, "now_str", lambda: clock.now.strftime("%Y-%m-%d %H:%M:%S"))
    db.confirm_group_notification_channel("qqbot", "10001", "markdown")
    client.send_group_media_result = AsyncMock(return_value=DeliveryResult(
        False, DeliveryCode.ACTIVE_FORBIDDEN, "qqbot", platform_code=40034105))
    assert not await bot._send_meme(message(), "10001", "question", proactive=True)
    assert not await bot._send_meme(message(), "10001", "question", proactive=True)
    assert client.send_group_media_result.await_count == 1
    assert memes.get_engine().recent_sent("10001") == []
    clock.now += timedelta(minutes=5)
    client.send_group_media_result.return_value = DeliveryResult(True, DeliveryCode.SENT, "qqbot")
    results = await asyncio.gather(*[
        bot._send_meme(message(), "10001", "question", proactive=True) for _ in range(3)])
    assert results.count(True) == 1
    assert client.send_group_media_result.await_count == 2
    assert db.get_group_notification_block("qqbot", "10001") is None
    assert db.get_group_notification_format("qqbot", "10001") == "markdown"


async def test_official_active_media_respects_daily_quota(monkeypatch):
    bot, client, api = official_media_bot(monkeypatch)
    clock = SimpleNamespace(now=datetime(2026, 11, 15, 10, tzinfo=db.TZ))
    monkeypatch.setattr(db, "_delivery_now", lambda value=None: value or clock.now)
    monkeypatch.setattr(db, "now_str", lambda: clock.now.strftime("%Y-%m-%d %H:%M:%S"))
    db.block_group_notifications("qqbot", "10001", DeliveryCode.QUOTA_EXHAUSTED.value, 11253)
    clock.now += timedelta(minutes=10)
    assert not await bot._send_meme(message(), "10001", "question", proactive=True)
    client.upload_group_media.assert_not_awaited()
    api.post_group_message.assert_not_awaited()
    clock.now += timedelta(days=1)
    assert await bot._send_meme(message(), "10001", "question", proactive=True)


@pytest.mark.parametrize("proactive", [False, True])
@pytest.mark.parametrize("stage", ["upload", "send"])
async def test_media_timeouts_are_bounded_and_not_recorded(monkeypatch, proactive, stage):
    bot, client, api = official_media_bot(monkeypatch)
    monkeypatch.setattr(notifications, "SEND_TIMEOUT_SECONDS", 0.01)
    cancelled = asyncio.Event()

    async def hang(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    if stage == "upload":
        client.upload_group_media = hang
    else:
        api.post_group_message.side_effect = hang
    result = await asyncio.wait_for(
        bot._send_meme(message(), "10001", "question", proactive=proactive), timeout=1)
    assert result is False and cancelled.is_set()
    assert memes.get_engine().recent_sent("10001") == []


async def test_missing_passive_msg_id_never_becomes_active(monkeypatch):
    bot, client, api = official_media_bot(monkeypatch)
    assert not await bot._send_meme(message(index=""), "10001", "question")
    client.upload_group_media.assert_not_awaited()
    api.post_group_message.assert_not_awaited()


@pytest.mark.parametrize("tag", ["[MEME:ac|wa_tle|question|slacking|interrupt]",
                                  "[MEME:问号]", "[MEME:ac-1]", "[MEME:ac"])
def test_invalid_or_truncated_model_tag_never_leaks_to_chat(tag):
    text, category = persona_chat.split_meme_tag("接住 " + tag)
    assert text == "接住"
    assert category == ""
