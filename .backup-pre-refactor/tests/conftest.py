"""离线测试公共配置：确保项目根目录可 import（config/database 等为顶层模块）。"""

import os
import sys
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def onebot_client():
    """OneBot 业务客户端：保留真实发送封装，只替换联网动作和常驻启动。"""
    from onebot_client import OneBotClient

    class FakeOneBotClient(OneBotClient):
        def __init__(self):
            super().__init__(ws_url="ws://127.0.0.1:3001")
            self.call_api = AsyncMock(return_value={
                "status": "ok", "retcode": 0, "data": {"message_id": 12345},
            })
            self.send_private_msg = AsyncMock(return_value=True)
            self.start = AsyncMock()

    return FakeOneBotClient()
