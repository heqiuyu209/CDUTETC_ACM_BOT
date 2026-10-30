"""实际连接 requirements 允许的 WebSocket 版本，不依赖 QQ。"""

import asyncio

import pytest
import websockets

from onebot_client import OneBotClient


@pytest.mark.parametrize("token", ["", "synthetic-token"])
async def test_connect_with_optional_authorization_header(token):
    received = asyncio.get_running_loop().create_future()

    async def server(ws):
        request = getattr(ws, "request", None)
        headers = request.headers if request is not None else ws.request_headers
        received.set_result(headers.get("Authorization"))
        await ws.close()

    async with websockets.serve(server, "127.0.0.1", 0) as listener:
        port = listener.sockets[0].getsockname()[1]
        client = OneBotClient(f"ws://127.0.0.1:{port}", token=token)
        await asyncio.wait_for(client._run_once(), 2)
        assert received.result() == ("Bearer synthetic-token" if token else None)
