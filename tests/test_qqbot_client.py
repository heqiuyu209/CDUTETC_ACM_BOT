"""官方机器人适配层测试：消息映射与发送行为，不需要安装 botpy。

重点验证：wrap_* 产出的对象字段与 OneBotMessage 对齐，且
always_addressed=True —— 否则 main 的「必须 @才响应」判断会把消息丢掉。
"""

from types import SimpleNamespace

from cdutetc.clients import qqbot
from cdutetc.clients.qqbot import QQBotClient, wrap_c2c_message, wrap_group_at_message


def test_group_message_maps_fields():
    message = SimpleNamespace(
        content="/帮助",
        group_openid="GROUP_OPENID",
        id="MSG123",
        author=SimpleNamespace(member_openid="MEMBER_OPENID"),
    )
    wrapped = wrap_group_at_message(message)

    assert wrapped.message_type == "group"
    assert wrapped.group_id == "GROUP_OPENID"
    assert wrapped.user_id == "MEMBER_OPENID"
    assert wrapped.content == "/帮助"
    assert wrapped.id == "MSG123"
    assert wrapped.always_addressed is True
    # raw 必须是字典（main 里会调用 raw.get），原始对象另存 source
    assert wrapped.raw == {}
    assert wrapped.source is message


def test_group_message_tolerates_missing_author():
    """botpy 版本差异导致 author 缺失时不能崩。"""
    message = SimpleNamespace(content="你好", group_openid="G", id="M")
    wrapped = wrap_group_at_message(message)
    assert wrapped.group_id == "G"
    assert wrapped.user_id == ""


def test_c2c_message_maps_fields():
    message = SimpleNamespace(
        content="你好",
        id="M1",
        author=SimpleNamespace(user_openid="USER_OPENID"),
    )
    wrapped = wrap_c2c_message(message)
    assert wrapped.message_type == "private"
    assert wrapped.user_id == "USER_OPENID"
    assert wrapped.always_addressed is True


def test_send_without_connection_returns_false():
    client = QQBotClient("appid", "secret")
    import asyncio

    assert asyncio.run(client.send_group_msg("g", "hi")) is False
    assert asyncio.run(client.send_private_msg("u", "hi")) is False


def test_send_group_is_active_without_explicit_reply_even_after_inbound_message():
    """最近收到的 msg_id 不得污染定时主动提醒。"""
    client = QQBotClient("appid", "secret")
    captured = {}

    class _Api:
        async def post_group_message(self, **params):
            captured.update(params)
            return {"id": "SENT1"}

    client._client = SimpleNamespace(api=_Api())
    # Active send stays independent of any previously received message.
    import asyncio

    result = asyncio.run(client.send_group_msg_result("g1", "提醒"))
    assert result and result.mode.value == "active"
    assert result.message_id == "SENT1"
    assert "msg_id" not in captured


def test_passive_reply_requires_explicit_message_id():
    client = QQBotClient("appid", "secret")
    captured = {}

    class _Api:
        async def post_group_message(self, **params):
            captured.update(params)
            return SimpleNamespace(id="SENT2")

    client._client = SimpleNamespace(api=_Api())
    # Passive context is explicit, never inferred from received message history.
    import asyncio

    result = asyncio.run(client.send_group_msg_result("g1", "reply", reply_to="EXPLICIT"))
    assert result and result.mode.value == "passive"
    assert captured["msg_id"] == "EXPLICIT"


def test_passive_failure_does_not_retry_as_active():
    client = QQBotClient("appid", "secret")
    calls = []

    class _Api:
        async def post_group_message(self, **params):
            calls.append(dict(params))
            raise RuntimeError("failure")

    client._client = SimpleNamespace(api=_Api())
    import asyncio

    result = asyncio.run(client.send_group_msg_result("g1", "reply", reply_to="M1"))
    assert not result and result.mode.value == "passive"
    assert len(calls) == 1 and calls[0]["msg_id"] == "M1"


def test_success_requires_message_id_receipt():
    client = QQBotClient("appid", "secret")

    class _Api:
        async def post_group_message(self, **params):
            return {"content": "maybe sent"}

    client._client = SimpleNamespace(api=_Api())
    import asyncio

    result = asyncio.run(client.send_group_msg_result("g1", "text"))
    assert not result and result.code.value == "unknown"


def test_private_active_send_does_not_use_last_inbound_id():
    client = QQBotClient("appid", "secret")
    captured = {}

    class _Api:
        async def post_c2c_message(self, **params):
            captured.update(params)
            return {"id": "SENT3"}

    client._client = SimpleNamespace(api=_Api())
    import asyncio

    assert asyncio.run(client.send_private_msg("u1", "notice")) is True
    assert "msg_id" not in captured


def test_api_call_failure_is_swallowed():
    """发送异常不能抛出，否则会中断提醒循环。"""
    client = QQBotClient("appid", "secret")

    class _Api:
        async def post_group_message(self, **params):
            raise RuntimeError("配额用尽")

    client._client = SimpleNamespace(api=_Api())

    import asyncio

    assert asyncio.run(client.send_group_msg("g1", "hi")) is False
