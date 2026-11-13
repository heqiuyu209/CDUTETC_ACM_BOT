"""P0 发信稳定性止血：配额码熔断、被动回复 seq 封顶、发信超时保护。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cdutetc import config, database, main, notifications
from cdutetc.errors import DeliveryCode, classify_exception
from cdutetc.clients.qqbot import QQBotClient


class ServerError(RuntimeError):
    """模拟 botpy 的 ServerError，业务码放在 msgs/响应体里。"""

    def __init__(self, payload):
        self.msgs = payload


def test_quota_error_codes_are_classified():
    assert classify_exception(ServerError({"code": 11253})) is DeliveryCode.QUOTA_EXHAUSTED
    assert classify_exception(ServerError({"err_code": 11263})) is DeliveryCode.QUOTA_EXHAUSTED
    # 老版本 SDK 只把文本留在 msgs 里，也要能识别
    assert classify_exception(ServerError("主动消息发送失败 code=11253")) is DeliveryCode.QUOTA_EXHAUSTED
    # 其他已知码仍保持原有分类，不被配额分支吞掉
    assert classify_exception(ServerError({"code": 40034100})) is DeliveryCode.RATE_LIMITED
    assert classify_exception(ServerError({"code": 40034105})) is DeliveryCode.ACTIVE_FORBIDDEN


def test_passive_reply_seq_is_capped_and_overflow_merged():
    assert main._clamp_reply_seq(1) == 1
    assert main._clamp_reply_seq(5) == 5
    assert main._clamp_reply_seq(6) == 5
    assert main._clamp_reply_seq(99) == 5

    texts = [f"第 {index} 段" for index in range(1, 7)]
    merged = main._merge_group_reply_texts(texts)
    assert len(merged) == 5
    assert merged[:4] == texts[:4]
    assert "第 5 段" in merged[4] and "第 6 段" in merged[4]
    assert merged[4].endswith("[消息过长已折叠]")
    # 未超上限时保持原样
    assert main._merge_group_reply_texts(texts[:5]) == texts[:5]


@pytest.mark.parametrize("reply_to", [None, "msg-1"])
async def test_send_timeout_becomes_network_error(monkeypatch, reply_to):
    monkeypatch.setattr(config, "BOT_PROTOCOL", "qqbot")
    monkeypatch.setattr(notifications, "SEND_TIMEOUT_SECONDS", 0.01)

    async def never_returns(*args, **kwargs):
        await asyncio.sleep(30)

    client = SimpleNamespace(send_group_msg_result=never_returns,
                             send_private_msg_result=never_returns)
    group = await notifications.send_group(client, "g1", "hello", reply_to=reply_to)
    private = await notifications.send_private(client, "u1", "hello", reply_to=reply_to)

    for result in (group, private):
        assert not result
        assert result.code is DeliveryCode.NETWORK_ERROR
        assert result.detail == "发信超时(10s)"


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BOT_PROTOCOL", "qqbot")
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1"})
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "p0.sqlite"))
    database.init_db()

    async def quota_exhausted(**kwargs):
        raise ServerError({"code": 11253, "message": "主动消息配额已耗尽"})

    api = SimpleNamespace(post_group_message=AsyncMock(side_effect=quota_exhausted))
    client = QQBotClient("offline-app", "offline-secret")
    client._client = SimpleNamespace(api=api)
    return SimpleNamespace(bot=main.ACMBot(client), api=api)


async def test_passive_reply_never_exceeds_five_sequences(bot):
    bot.api.post_group_message = AsyncMock(return_value={"id": "sent-id"})
    main._reply_sequence.set(0)
    message = SimpleNamespace(group_id="g1", id="incoming-msg")

    for _ in range(7):
        assert await bot.bot._safe_reply(message, "分段回复")

    seqs = [call.kwargs["msg_seq"] for call in bot.api.post_group_message.await_args_list]
    assert seqs == [1, 2, 3, 4, 5, 5, 5]


async def test_quota_exhausted_blocks_group_and_stops_retry(bot, caplog):
    with caplog.at_level("WARNING"):
        result = await bot.bot._send_group_result("g1", "今天有周赛")

    assert result.code is DeliveryCode.QUOTA_EXHAUSTED
    block = database.get_group_notification_block("qqbot", "g1")
    assert block and block["error_code"] == DeliveryCode.QUOTA_EXHAUSTED.value
    assert "今日主动消息配额已耗尽" in caplog.text

    # 熔断后不再打平台，避免定时任务持续无效重试
    before = bot.api.post_group_message.await_count
    again = await bot.bot._send_group_result("g1", "今天有周赛")
    assert again.code is DeliveryCode.ACTIVE_FORBIDDEN
    assert bot.api.post_group_message.await_count == before


async def test_quota_block_expires_next_day_and_is_refreshed(bot, monkeypatch):
    clock = ["2026-11-11 23:59:00"]
    monkeypatch.setattr(database, "now_str", lambda: clock[0])

    assert (await bot.bot._send_group_result("g1", "first")).code is DeliveryCode.QUOTA_EXHAUSTED
    assert database.get_group_notification_block("qqbot", "g1")["blocked_at"] == clock[0]
    assert (await bot.bot._send_group_result("g1", "same day")).code is DeliveryCode.ACTIVE_FORBIDDEN
    assert bot.api.post_group_message.await_count == 1

    clock[0] = "2026-11-12 00:00:00"
    assert database.get_group_notification_block("qqbot", "g1") is None
    assert (await bot.bot._send_group_result("g1", "next day")).code is DeliveryCode.QUOTA_EXHAUSTED
    assert bot.api.post_group_message.await_count == 2
    assert database.get_group_notification_block("qqbot", "g1")["blocked_at"] == clock[0]
    assert (await bot.bot._send_group_result("g1", "blocked again")).code is DeliveryCode.ACTIVE_FORBIDDEN


def test_quota_expiry_preserves_other_blocks_and_bad_dates(bot, monkeypatch):
    clock = ["2026-11-11 10:00:00"]
    monkeypatch.setattr(database, "now_str", lambda: clock[0])
    database.block_group_notifications("qqbot", "g1", DeliveryCode.QUOTA_EXHAUSTED.value, 11253)
    database.block_group_notifications("qqbot", "g2", DeliveryCode.ACTIVE_FORBIDDEN.value, 40034105)
    database.block_group_notifications("onebot", "g1", DeliveryCode.ACTIVE_FORBIDDEN.value, 40034105)
    clock[0] = "2026-11-12 10:00:00"
    assert database.get_group_notification_block("qqbot", "g1") is None
    assert database.get_group_notification_block("qqbot", "g2")
    assert database.get_group_notification_block("onebot", "g1")

    with database._connect() as conn:
        conn.execute("UPDATE group_notification_blocks SET blocked_at='invalid' WHERE protocol='qqbot' AND group_id='g1'")
    assert database.get_group_notification_block("qqbot", "g1")
    with database._connect() as conn:
        conn.execute("UPDATE group_notification_blocks SET blocked_at='2026-11-13 10:00:00' WHERE protocol='qqbot' AND group_id='g1'")
    assert database.get_group_notification_block("qqbot", "g1")
