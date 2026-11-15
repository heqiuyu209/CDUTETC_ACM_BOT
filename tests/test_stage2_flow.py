"""阶段二主流程接入测试：复读机/表情包在群消息链上的行为与优雅降级。"""

import sqlite3

import pytest

from cdutetc import chat_fun, config, database, main, memes
from cdutetc.clients.onebot import OneBotMessage
from cdutetc.errors import DeliveryResult


def _msg(content="破防了", group="g1", member="m1", message_id=12345):
    return OneBotMessage({"post_type": "message", "message_type": "group",
                          "raw_message": content, "group_id": group,
                          "user_id": member, "message_id": message_id,
                          "self_id": 30001})


@pytest.fixture(autouse=True)
def _fresh_engines():
    """每个用例从空的窗口与冷却开始。"""
    chat_fun.reset_engine()
    memes.reset_engine()
    yield
    chat_fun.reset_engine()
    memes.reset_engine()


@pytest.fixture
def db_conn(monkeypatch):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    database.init_db()
    yield conn
    conn.close()


@pytest.fixture
def bot(monkeypatch, onebot_client, db_conn):
    """OneBot 协议下的机器人：复读走被动回复，发图一律降级。"""
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"m1"})
    monkeypatch.setattr(config, "PUBLISH_CMD", "发布")
    monkeypatch.setattr(config, "BOT_QQ", "")
    monkeypatch.setattr(config, "GROUP_REQUIRE_AT", True)
    monkeypatch.setattr(config, "NL_INTENT_ENABLED", False)
    monkeypatch.setattr(config, "TEACHER_AUTO_INTENT_ENABLED", False)
    monkeypatch.setattr(config, "ECHO_ENABLED", True)
    monkeypatch.setattr(config, "MEME_ENABLED", True)
    monkeypatch.setattr(config, "MEME_BASE_URL", "")

    instance = main.ACMBot(onebot_client)
    replies = []

    async def fake_reply(message, content):
        replies.append(content)
        return True

    monkeypatch.setattr(instance, "_safe_reply", fake_reply)
    return instance, replies


class FakeQQBotClient:
    """官方协议客户端桩：只声明能力位与富媒体发送，不联任何网。"""

    protocol = "qqbot"
    supports_passive_reply = True
    supports_numeric_group_id = False
    supports_markdown = True
    self_id = "BOT-OPENID"

    def __init__(self):
        from unittest.mock import AsyncMock

        self.upload_group_media = AsyncMock(return_value=("FILE-INFO-1", 1800))
        self.send_group_media_result = AsyncMock(
            return_value=DeliveryResult(True, "ok", "qqbot"),
        )


@pytest.fixture
def qqbot(monkeypatch, db_conn):
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"G-OPENID"})
    monkeypatch.setattr(config, "TEACHER_AUTO_INTENT_ENABLED", False)
    monkeypatch.setattr(config, "ECHO_ENABLED", True)
    monkeypatch.setattr(config, "MEME_ENABLED", True)
    monkeypatch.setattr(config, "MEME_BASE_URL", "https://cdn.example.com/memes")

    client = FakeQQBotClient()
    instance = main.ACMBot(client)
    replies = []

    async def fake_reply(message, content):
        replies.append(content)
        return True

    monkeypatch.setattr(instance, "_safe_reply", fake_reply)
    return instance, client, replies


def _use_assets(monkeypatch, root):
    """把表情包单例指向临时素材目录，并固定随机源为必中。"""
    engine = memes.MemeEngine(root=root, rng=lambda: 0.0)
    monkeypatch.setattr(memes, "_default_engine", engine)
    return engine


# ---------- 复读 ----------


async def test_three_members_same_line_echoes(bot):
    b, replies = bot
    await b.on_group_at_message_create(_msg("破防了", member="m1"))
    await b.on_group_at_message_create(_msg("破防了", member="m2"))
    assert replies == []
    await b.on_group_at_message_create(_msg("破防了", member="m3"))
    assert replies == ["破防了"]


async def test_echo_respects_switch(bot, monkeypatch):
    b, replies = bot
    monkeypatch.setattr(config, "ECHO_ENABLED", False)
    monkeypatch.setattr(config, "MEME_ENABLED", False)
    for member in ("m1", "m2", "m3"):
        await b.on_group_at_message_create(_msg("破防了", member=member))
    assert replies == []


async def test_same_member_spam_never_echoes(bot):
    b, replies = bot
    for _ in range(3):
        await b.on_group_at_message_create(_msg("破防了", member="m1"))
    assert replies == []


async def test_teacher_consumed_message_never_echoes(bot, monkeypatch):
    """被老师意图管线接管的消息不进复读链路（互斥）。"""
    b, replies = bot
    monkeypatch.setattr(config, "TEACHER_AUTO_INTENT_ENABLED", True)

    async def fake_handle(text, **kwargs):
        return {"action": "saved", "reply": "已登记"}

    monkeypatch.setattr(main.teacher_intent, "handle_message", fake_handle)
    for member in ("m1", "m2", "m3"):
        await b.on_group_at_message_create(_msg("破防了", member=member))
    assert replies == ["已登记"] * 3


# ---------- 表情包降级 ----------


async def test_meme_skipped_on_onebot_protocol(bot):
    """OneBot 拼不出 file_info：完全不发图，也不影响主流程。"""
    b, replies = bot
    assert await b._send_meme(_msg("WA了"), "g1", memes.CATEGORY_WA_TLE) is False
    assert replies == []


async def test_meme_skipped_without_base_url(qqbot, tmp_path, monkeypatch):
    """未配置 ACM_MEME_BASE_URL 时跳过发图。"""
    b, client, _ = qqbot
    monkeypatch.setattr(config, "MEME_BASE_URL", "")
    _use_assets(monkeypatch, _assets(tmp_path))
    assert await b._send_meme(_msg("WA了", group="G-OPENID"), "G-OPENID",
                              memes.CATEGORY_WA_TLE) is False
    client.upload_group_media.assert_not_awaited()
    client.send_group_media_result.assert_not_awaited()


async def test_meme_skipped_without_assets(qqbot, tmp_path, monkeypatch):
    """素材目录缺失时静默跳过，不调上传接口。"""
    b, client, _ = qqbot
    _use_assets(monkeypatch, tmp_path / "空目录")
    assert await b._send_meme(_msg("WA了", group="G-OPENID"), "G-OPENID",
                              memes.CATEGORY_WA_TLE) is False
    client.upload_group_media.assert_not_awaited()


async def test_meme_upload_then_cache_reuse(qqbot, tmp_path, monkeypatch):
    """同一群同一张图：首次上传换 file_info，第二次直接复用缓存。"""
    b, client, _ = qqbot
    _use_assets(monkeypatch, _assets(tmp_path))
    message = _msg("WA了", group="G-OPENID")
    assert await b._send_meme(message, "G-OPENID", memes.CATEGORY_WA_TLE) is True
    assert await b._send_meme(message, "G-OPENID", memes.CATEGORY_WA_TLE) is True
    client.upload_group_media.assert_awaited_once()
    assert client.send_group_media_result.await_count == 2
    assert database.get_meme_file_cache("G-OPENID", "wa_tle/1.png") == "FILE-INFO-1"


async def test_meme_upload_failure_is_silent(qqbot, tmp_path, monkeypatch):
    """上传接口失败：不发送、不抛异常。"""
    b, client, _ = qqbot
    _use_assets(monkeypatch, _assets(tmp_path))
    client.upload_group_media.return_value = None
    assert await b._send_meme(_msg("WA了", group="G-OPENID"), "G-OPENID",
                              memes.CATEGORY_WA_TLE) is False
    client.send_group_media_result.assert_not_awaited()


async def test_keyword_message_triggers_meme(qqbot, tmp_path, monkeypatch):
    """群里出现 WA 关键词且随机命中 → 走完整链路发出表情包。"""
    b, client, replies = qqbot
    _use_assets(monkeypatch, _assets(tmp_path))
    await b.on_group_at_message_create(_msg("这题又 WA了", group="G-OPENID", member="m9"))
    client.upload_group_media.assert_awaited_once()
    assert client.send_group_media_result.await_count == 1
    assert replies == []


def _assets(root):
    """在临时目录里造一份素材库（每分类一张图）。"""
    for category in memes.CATEGORIES:
        directory = root / category
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "1.png").write_bytes(b"fake-png")
    return root
