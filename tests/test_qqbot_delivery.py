"""Official QQ adapter: explicit passive reply and receipt contract."""
import asyncio
from types import SimpleNamespace

from cdutetc.clients.qqbot import QQBotClient


def test_group_default_active_even_with_old_style_history_attributes():
    client = QQBotClient("app", "secret")
    calls = []
    class Api:
        async def post_group_message(self, **params):
            calls.append(params)
            return {"id": "sent-1"}
    client._client = SimpleNamespace(api=Api())
    result = asyncio.run(client.send_group_msg_result("g", "reminder"))
    assert result and result.mode.value == "active"
    assert "msg_id" not in calls[0]


def test_explicit_passive_reply_has_seq_and_no_active_retry():
    client = QQBotClient("app", "secret")
    calls = []
    class Api:
        async def post_group_message(self, **params):
            calls.append(params)
            raise RuntimeError("failure")
    client._client = SimpleNamespace(api=Api())
    result = asyncio.run(client.send_group_msg_result("g", "reply", reply_to="in-1", reply_seq=3))
    assert not result and result.mode.value == "passive"
    assert len(calls) == 1
    assert calls[0]["msg_id"] == "in-1" and calls[0]["msg_seq"] == 3


def test_c2c_passive_reply_has_seq_and_active_private_has_no_msg_id():
    client = QQBotClient("app", "secret")
    calls = []
    class Api:
        async def post_c2c_message(self, **params):
            calls.append(params)
            return SimpleNamespace(id="out-1")
    client._client = SimpleNamespace(api=Api())
    active = asyncio.run(client.send_private_msg_result("u", "notice"))
    passive = asyncio.run(client.send_private_msg_result("u", "reply", reply_to="in-1", reply_seq=2))
    assert active and "msg_id" not in calls[0]
    assert passive and calls[1]["msg_id"] == "in-1" and calls[1]["msg_seq"] == 2


def test_missing_or_invalid_receipt_never_means_sent():
    client = QQBotClient("app", "secret")
    class Api:
        async def post_group_message(self, **params):
            return None
    client._client = SimpleNamespace(api=Api())
    result = asyncio.run(client.send_group_msg_result("g", "text"))
    assert not result and result.code.value == "timeout_unknown"


def test_empty_message_and_invalid_reply_sequence_are_rejected():
    client = QQBotClient("app", "secret")
    assert asyncio.run(client.send_group_msg_result("g", "")).code.value == "empty_message"
    assert asyncio.run(client.send_group_msg_result("g", "x", reply_to="m", reply_seq=0)).code.value == "invalid_request"
