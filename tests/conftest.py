"""离线测试公共配置：确保项目根目录中的 cdutetc 包可 import。"""

import os
import re
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def official_client():
    """保留官方发送封装，只模拟平台 API 与常驻启动。"""
    from cdutetc.clients.qqbot import QQBotClient

    client = QQBotClient("offline-app", "offline-secret")
    client.api = SimpleNamespace(
        post_group_message=AsyncMock(return_value={"id": "group-sent"}),
        post_c2c_message=AsyncMock(return_value={"id": "private-sent"}),
    )
    client._client = SimpleNamespace(api=client.api)
    client.start = AsyncMock()
    return client


def official_message(*, content="", group_id="", user_id="", msg_id="",
                     message_type="group", bot_id="30001", mentions=None,
                     attachments=()):
    """合成官方事件并经过真实适配层；bot_id 仅用于生成测试平台的 is_you 字段。"""
    from cdutetc.clients.qqbot import wrap_c2c_message, wrap_group_message

    if message_type == "private":
        return wrap_c2c_message(SimpleNamespace(
            id=msg_id, content=content,
            author=SimpleNamespace(user_openid=str(user_id)),
        ))
    if mentions is None:
        mentions = [{"id": target, "is_you": target == str(bot_id)}
                    for target in re.findall(r"<@(?!\!)([^>\s]+)>", content)]
    return wrap_group_message({
        "id": msg_id, "group_openid": group_id, "content": content,
        "author": {"member_openid": user_id},
        "mentions": mentions, "attachments": list(attachments),
    })
