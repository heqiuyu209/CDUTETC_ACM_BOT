"""官方机器人适配层测试：消息映射与发送行为，不需要安装 botpy。

重点验证：wrap_* 产出的对象字段与 OneBotMessage 对齐，且
always_addressed=True —— 否则 main 的「必须 @才响应」判断会把消息丢掉。
"""

from types import SimpleNamespace

import qqbot_client
from qqbot_client import QQBotClient, wrap_c2c_message, wrap_group_at_message


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


def test_send_group_uses_msg_id_for_passive_reply():
    """群内收到过消息时应带 msg_id 做被动回复，避免消耗主动配额。"""
    client = QQBotClient("appid", "secret")
    captured = {}

    class _Api:
        async def post_group_message(self, **params):
            captured.update(params)
            return {"id": "x"}

    client._client = SimpleNamespace(api=_Api())
    client._last_msg_id["g1"] = "MSG1"

    import asyncio

    assert asyncio.run(client.send_group_msg("g1", "hi")) is True
    assert captured["group_openid"] == "g1"
    assert captured["msg_id"] == "MSG1"
    assert captured["content"] == "hi"


def test_send_group_retries_without_msg_id_on_typeerror():
    """某些 botpy 版本不接受 msg_id，应退化重试一次而不是直接失败。"""
    client = QQBotClient("appid", "secret")
    calls = []

    class _Api:
        async def post_group_message(self, **params):
            calls.append(dict(params))
            if "msg_id" in params:
                raise TypeError("unexpected keyword argument 'msg_id'")
            return {"id": "x"}

    client._client = SimpleNamespace(api=_Api())
    client._last_msg_id["g1"] = "MSG1"

    import asyncio

    assert asyncio.run(client.send_group_msg("g1", "hi")) is True
    assert len(calls) == 2
    assert "msg_id" in calls[0] and "msg_id" not in calls[1]


def test_api_call_failure_is_swallowed():
    """发送异常不能抛出，否则会中断提醒循环。"""
    client = QQBotClient("appid", "secret")

    class _Api:
        async def post_group_message(self, **params):
            raise RuntimeError("配额用尽")

    client._client = SimpleNamespace(api=_Api())

    import asyncio

    assert asyncio.run(client.send_group_msg("g1", "hi")) is False
