"""本地真实 WebSocket 回归，覆盖事件内回执、顺序和断线清理。"""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
import websockets

from onebot_client import OneBotClient


def event(kind="group", message_id=1):
    return json.dumps({"post_type": "message", "message_type": kind,
                       "message_id": message_id, "group_id": 123,
                       "user_id": 456, "raw_message": "/帮助"})


def receipt(action):
    return json.dumps({"status": "ok", "retcode": 0,
                       "data": {"message_id": 99}, "echo": action["echo"]})


@asynccontextmanager
async def running_client(server_handler, configure):
    async with websockets.serve(server_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = OneBotClient(f"ws://127.0.0.1:{port}")
        configure(client)
        task = asyncio.create_task(client._run_once())
        try:
            yield client, task
        finally:
            if client._ws is not None:
                await client._ws.close()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("kind", ["group", "private"])
async def test_event_reply_consumes_success_receipt_without_timeout(kind):
    done = asyncio.get_running_loop().create_future()

    async def server(ws):
        await ws.send(event(kind))
        action = json.loads(await ws.recv())
        assert action["action"] == f"send_{kind}_msg"
        await ws.send(receipt(action))
        await ws.wait_closed()

    def configure(client):
        async def handle(message):
            send = client.send_group_msg if kind == "group" else client.send_private_msg
            target = message.group_id if kind == "group" else message.user_id
            done.set_result(await send(target, "本地回复"))
        setattr(client, f"{kind}_msg_handler", handle)

    async with running_client(server, configure) as (client, task):
        assert await asyncio.wait_for(done, 2) is True
        assert client._pending == {}


async def test_ready_callback_can_call_api_and_events_keep_arrival_order():
    handled = []
    done = asyncio.Event()
    ready_receipt = []

    async def server(ws):
        await ws.send(event(message_id=1))
        await ws.send(event(message_id=2))
        async for raw in ws:
            await ws.send(receipt(json.loads(raw)))

    def configure(client):
        async def ready():
            ready_receipt.append(await client.send_private_msg("456", "就绪"))
        async def handle(message):
            assert await client.send_group_msg(message.group_id, message.id)
            handled.append(message.id)
            if len(handled) == 2:
                done.set()
        client.on_ready = ready
        client.group_msg_handler = handle

    async with running_client(server, configure):
        await asyncio.wait_for(done.wait(), 2)
        assert ready_receipt == [True]
        assert handled == ["1", "2"]


async def test_failed_handler_does_not_stop_next_event(caplog):
    done = asyncio.Event()

    async def server(ws):
        await ws.send(event(message_id=1))
        await ws.send(event(message_id=2))
        await ws.wait_closed()

    def configure(client):
        async def handle(message):
            if message.id == "1":
                raise RuntimeError("synthetic handler failure")
            done.set()
        client.group_msg_handler = handle

    async with running_client(server, configure):
        await asyncio.wait_for(done.wait(), 2)
        assert "OneBot 事件处理失败" in caplog.text


async def test_disconnect_completes_pending_calls_and_clears_session():
    started = asyncio.Event()

    async def server(ws):
        await ws.recv()
        await ws.close()

    def configure(client):
        async def ready():
            started.set()
        client.on_ready = ready

    async with running_client(server, configure) as (client, task):
        await asyncio.wait_for(started.wait(), 2)
        assert await asyncio.wait_for(client.send_group_msg("123", "断线"), 2) is False
        await asyncio.wait_for(task, 2)
        assert client._ws is None
        assert client._pending == {}
        assert await client.call_api("send_group_msg") is None


async def test_cancelled_call_removes_pending_future():
    received = asyncio.Event()
    ready = asyncio.Event()

    async def server(ws):
        await ws.recv()
        received.set()
        await ws.wait_closed()

    def configure(client):
        async def on_ready():
            ready.set()
        client.on_ready = on_ready

    async with running_client(server, configure) as (client, task):
        await asyncio.wait_for(ready.wait(), 2)
        call = asyncio.create_task(client.call_api("send_group_msg"))
        await asyncio.wait_for(received.wait(), 2)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert client._pending == {}


async def test_invalid_payloads_do_not_break_receipt_processing():
    ready = asyncio.Event()

    async def server(ws):
        action = json.loads(await ws.recv())
        for raw in ["not JSON", "null", "[]", '{"echo": {}}']:
            await ws.send(raw)
        await ws.send(receipt(action))
        await ws.wait_closed()

    def configure(client):
        async def on_ready():
            ready.set()
        client.on_ready = on_ready

    async with running_client(server, configure) as (client, task):
        await asyncio.wait_for(ready.wait(), 2)
        assert await asyncio.wait_for(client.send_group_msg("123", "有效回复"), 2)


async def test_queue_is_bounded_and_overflow_does_not_wait_for_space():
    client = OneBotClient("ws://127.0.0.1:1", event_queue_size=1)
    client._event_queue = asyncio.Queue(maxsize=1)
    client.group_msg_handler = lambda message: None
    await client._dispatch(event(message_id=1))
    with pytest.raises(RuntimeError, match="event queue is full"):
        await asyncio.wait_for(client._dispatch(event(message_id=2)), 0.5)
    assert client._event_queue.qsize() == 1
