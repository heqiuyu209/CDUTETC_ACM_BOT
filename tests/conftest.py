"""离线测试公共配置：确保项目根目录中的 cdutetc 包可 import。"""

import os
import sys
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def onebot_client():
    """OneBot 业务客户端：保留真实发送封装，只替换联网动作和常驻启动。"""
    from cdutetc.clients.onebot import OneBotClient

    class FakeOneBotClient(OneBotClient):
        def __init__(self):
            super().__init__(ws_url="ws://127.0.0.1:3001")
            self.call_api = AsyncMock(return_value={
                "status": "ok", "retcode": 0, "data": {"message_id": 12345},
            })
            self._ws = object()
            # 自身标识统一由客户端属性提供（生产代码已不再读事件 raw 的 self_id），
            # 离线测试固定登录账号为 30001。
            self.self_id = "30001"
            self.send_private_msg = AsyncMock(return_value=True)
            self.start = AsyncMock()

    return FakeOneBotClient()


@pytest.fixture(autouse=True)
def _default_bot_identity(monkeypatch):
    """离线测试统一把机器人自身账号当作 30001。

    生产代码已不再从事件 raw 的 self_id 猜自身标识，只认客户端属性或配置；
    需要验证「取不到自身标识」的用例在该用例内自行把 BOT_QQ 覆盖为空串。
    """
    from cdutetc import config

    monkeypatch.setattr(config, "BOT_QQ", "30001")
