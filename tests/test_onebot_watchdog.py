"""OneBot 连接层回归测试：静默看门狗必须能兜住 TCP 半开连接。

背景：读循环是 `async for raw in ws`，当连接变成半开（对端已死但本端
未收到 FIN/RST）时会永远阻塞——不抛异常、不重连、也不回消息，
表现为「没人动它，机器人自己就不说话了」。
"""

import asyncio
import time

import pytest

from onebot_client import OneBotClient


class _FakeWS:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


def test_idle_timeout_closes_silent_connection():
    """长时间没有任何事件时必须主动断开，交给外层重连。"""

    async def run():
        client = OneBotClient("ws://unused", idle_timeout=0.3)
        ws = _FakeWS()
        client._last_rx = time.monotonic() - 10
        task = asyncio.create_task(client._idle_watchdog(ws))
        await asyncio.sleep(1.5)
        task.cancel()
        return ws.closed

    assert asyncio.run(run()) is True


def test_idle_timeout_keeps_healthy_connection():
    """持续有事件（心跳）时不能误断。"""

    async def run():
        client = OneBotClient("ws://unused", idle_timeout=0.4)
        ws = _FakeWS()
        client._last_rx = time.monotonic()
        task = asyncio.create_task(client._idle_watchdog(ws))
        for _ in range(8):
            await asyncio.sleep(0.2)
            client._last_rx = time.monotonic()
        task.cancel()
        return ws.closed

    assert asyncio.run(run()) is False


def test_idle_timeout_zero_disables_watchdog():
    """设为 0 表示关闭看门狗，立即返回不关闭连接。"""

    async def run():
        client = OneBotClient("ws://unused", idle_timeout=0)
        ws = _FakeWS()
        await client._idle_watchdog(ws)
        return ws.closed

    assert asyncio.run(run()) is False
