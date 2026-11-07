"""Official QQ adapter: explicit passive reply and receipt contract."""
import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

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


async def test_group_active_sends_have_independent_sequences_and_explicit_markdown_payload():
    api = SimpleNamespace(post_group_message=AsyncMock(return_value={"id": "accepted"}))
    client = QQBotClient("app", "secret")
    client._client = SimpleNamespace(api=api)
    await client.send_group_msg_result("g", "第一条提醒")
    await client.send_group_msg_result("g", "当前消息回复", reply_to="fresh", reply_seq=3)
    await client.send_group_msg_result("g", "第二条提醒", message_format="markdown")
    text, passive, markdown = [call.kwargs for call in api.post_group_message.await_args_list]
    assert text["msg_type"] == 0 and text["content"] == "第一条提醒"
    assert markdown["msg_type"] == 2 and markdown["markdown"] == {"content": "第二条提醒"}
    assert "content" not in markdown
    assert "msg_id" not in text and "msg_id" not in markdown
    assert all(1 <= params["msg_seq"] <= 2**31 - 1 for params in (text, markdown))
    assert text["msg_seq"] != markdown["msg_seq"]
    assert passive["msg_id"] == "fresh" and passive["msg_seq"] == 3
    assert not await client.send_group_msg_result("g", "不应发送", message_format="html")
    assert api.post_group_message.await_count == 3


@pytest.fixture
def state_client(monkeypatch):
    http_module = ModuleType("botpy.http")
    http_module.Route = lambda method, path, **params: SimpleNamespace(method=method, path=path, params=params)
    monkeypatch.setitem(sys.modules, "botpy", ModuleType("botpy"))
    monkeypatch.setitem(sys.modules, "botpy.http", http_module)
    http = SimpleNamespace(request=AsyncMock())
    client = QQBotClient("app", "secret")
    client._client = SimpleNamespace(api=SimpleNamespace(_http=http))
    return client, http


@pytest.mark.parametrize("allowed", [True, False])
async def test_bot_state_reads_target_group_switch_without_sending(state_client, allowed):
    client, http = state_client
    http.request.return_value = {"allow_proactive_msg": allowed, "member_openid": "private-identity"}
    state = await client.get_group_active_permission("target-group")
    assert state.allowed is allowed and state.code == "ok"
    route = http.request.await_args.args[0]
    assert route.method == "GET" and route.path == "/v2/groups/{group_openid}/bot_state"
    assert route.params == {"group_openid": "target-group"}
    assert "private-identity" not in repr(state)


@pytest.mark.parametrize("payload", [None, {}, {"allow_proactive_msg": "false"}, {"allow_proactive_msg": 1}])
async def test_missing_or_malformed_bot_state_is_unknown_not_disabled(state_client, payload):
    client, http = state_client
    http.request.return_value = payload
    state = await client.get_group_active_permission("g")
    assert state.allowed is None and state.code == "unknown"


async def test_bot_state_permission_error_and_timeout_do_not_claim_switch_is_closed(state_client):
    client, http = state_client
    http.request.return_value = {"code": 11253, "message": "private request data"}
    state = await client.get_group_active_permission("g")
    assert state.allowed is None and state.platform_code == 11253
    assert "private request data" not in repr(state)
    http.request.side_effect = TimeoutError("private timeout data")
    state = await client.get_group_active_permission("g")
    assert state.allowed is None and state.code == "timeout_unknown"
    assert "private timeout data" not in repr(state)


async def test_bot_state_missing_sdk_support_and_invalid_target_are_reported():
    client = QQBotClient("app", "secret")
    assert (await client.get_group_active_permission("g")).code == "not_connected"
    assert (await client.get_group_active_permission("")).code == "invalid_target"
    client._client = SimpleNamespace(api=SimpleNamespace())
    assert (await client.get_group_active_permission("g")).code == "unsupported"
