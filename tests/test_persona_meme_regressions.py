"""聊天开关、过滤与官方协议收发图的入口回归；仅模拟外部 I/O。"""
import asyncio
import io
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cdutetc import config, content_filter, database as db, main, memes, notifications, persona_chat
from conftest import official_message
from cdutetc.clients.qqbot import QQBotClient, wrap_group_message, wrap_group_at_message
from cdutetc.errors import DeliveryCode, DeliveryResult


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(db, "_connect", lambda: conn)
    db.init_db()
    memes.reset_collect_limits()
    monkeypatch.setattr(memes, "extract_image_text", lambda data: "")
    values = {
        "ALLOWED_GROUPS": {"10001"},
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
    memes.reset_collect_limits()
    memes.reset_engine()
    persona_chat.reset_engine()


def message(text="在吗", *, at=True, member="u1", index=1):
    prefix = "<@30001> " if at else ""
    return official_message(message_type="group", msg_id=index, group_id="10001", user_id=member, content=prefix + text)


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
    client = QQBotClient("offline", "offline")
    client.upload_group_media = AsyncMock(return_value=("file-info", 1800))
    client.send_group_media_result = AsyncMock(return_value=DeliveryResult.sent("qqbot", main.DeliveryMode.ACTIVE, "accepted"))
    bot = main.ACMBot(client)
    for index in (1, 2):
        await bot.on_group_at_message_create(message("？", at=False, index=index))
    client.upload_group_media.assert_not_awaited()
    client.send_group_media_result.assert_not_awaited()


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
    assert chat.await_args.args[1].count(f"唯一文本{persona_chat.INTERJECT_MIN_MESSAGES - 1}") == 1


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
    stored = db.get_meme_hash(memes.image_md5(image.getvalue()))
    with Image.open(tmp_path / stored["file_path"]) as saved:
        assert saved.format == "JPEG" and saved.size == (200, 200)
        assert saved.getpixel((100, 100))[2] > 240  # 压缩后仍是蓝色图片


def test_non_image_official_attachment_is_ignored():
    wrapped = wrap_group_message({"attachments": [SimpleNamespace(
        url="https://example.invalid/a.gif", filename="a.gif", content_type="application/pdf")]})
    assert memes.extract_image_refs(wrapped) == []




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
    # Python 3.12 的 wait_for 不保证额外让出事件循环。显式挂住平台回执，
    # 才能验证「恢复尚未确认时只有一个请求在途」，而不是依赖 gather 的调度顺序。
    started, release = asyncio.Event(), asyncio.Event()

    async def pending_receipt(*args, **kwargs):
        started.set()
        await release.wait()
        return DeliveryResult(True, DeliveryCode.SENT, "qqbot")

    client.send_group_media_result.side_effect = pending_receipt
    probe = asyncio.create_task(bot._send_meme(message(), "10001", "question", proactive=True))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        assert db.get_group_notification_block("qqbot", "10001") is not None
        results = await asyncio.gather(*[
            bot._send_meme(message(), "10001", "question", proactive=True) for _ in range(2)])
        assert results == [False, False]
        assert client.send_group_media_result.await_count == 2
    finally:
        release.set()
        recovered = await asyncio.wait_for(probe, timeout=1)
    assert recovered
    assert client.send_group_media_result.await_count == 2
    assert db.get_group_notification_block("qqbot", "10001") is None
    assert db.get_group_notification_format("qqbot", "10001") == "markdown"
    # 成功回执解除暂停，后续消息必须恢复正常发送，不能把整批误当成只准发一条。
    assert await bot._send_meme(message(), "10001", "question", proactive=True)
    assert client.send_group_media_result.await_count == 3


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


async def test_gif_without_url_extension_keeps_animation(monkeypatch, tmp_path):
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (200, 200), "blue").save(
        buffer, "GIF", save_all=True,
        append_images=[Image.new("RGB", (200, 200), "red")], duration=100, loop=0)
    data = buffer.getvalue()
    result = await memes.collect_one_image(
        {"url": "https://example.invalid/download?fileid=123", "file": "image-id"},
        group_id="10001", root=tmp_path, downloader=AsyncMock(return_value=data))
    assert result is not None
    stored = (tmp_path / result["relative_path"]).read_bytes()
    assert stored == data
    with Image.open(io.BytesIO(stored)) as image:
        assert image.n_frames == 2


@pytest.mark.parametrize("proactive", [False, True])
async def test_cancelling_media_send_cancels_network_task(monkeypatch, proactive):
    bot, client, api = official_media_bot(monkeypatch)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def hang(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    api.post_group_message.side_effect = hang
    task = asyncio.create_task(bot._send_meme(message(), "10001", "question", proactive=proactive))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert memes.get_engine().recent_sent("10001") == []


async def test_official_active_image_failure_stays_isolated(monkeypatch):
    monkeypatch.setattr(config, "MEME_ENABLED", True)
    client = QQBotClient("offline", "offline")
    client.upload_group_media = AsyncMock(return_value=("file-info", 1800))
    client.send_group_media_result = AsyncMock(side_effect=RuntimeError("offline send failed"))
    bot = main.ACMBot(client)
    assert not await bot._send_meme(message(), "10001", "question", proactive=True)
    assert memes.get_engine().recent_sent("10001") == []


def test_official_nickname_survives_normalization():
    wrapped = wrap_group_message({"author": SimpleNamespace(member_openid="u1", username="小明")})
    assert main.ACMBot()._nickname_of(wrapped, "u1") == "小明"
