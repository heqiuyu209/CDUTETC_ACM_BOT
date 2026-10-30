"""OneBot 11 正向 WebSocket 适配层：连接 NapCatQQ（普通 QQ 账号），收发群/私聊消息。

设计：
- 机器人作为客户端连接 NapCat 的正向 WebSocket（如 ws://127.0.0.1:3001）。
- 收到 message 事件后包装为 OneBotMessage，分发给注册的 handler。
- call_api 发送 OneBot 动作（send_group_msg / send_private_msg 等）并等待回执。
- 断线自动重连，心跳等 meta 事件自动忽略。
"""

import asyncio
import json
import logging
from typing import Awaitable, Callable

import websockets

logger = logging.getLogger("onebot")

MessageHandler = Callable[["OneBotMessage"], Awaitable[None]]
ReadyHandler = Callable[[], Awaitable[None]]


class OneBotMessage:
    """OneBot 消息统一封装，兼容旧业务代码对 message 的字段访问。

    旧版 botpy 的 message.group_openid/member_openid 在 OneBot 中对应：
      group_id（QQ 群号）、user_id（QQ 号），统一转字符串便于白名单比较。
    """

    __slots__ = ("raw", "id", "message_type", "group_id", "user_id", "content")

    def __init__(self, data: dict):
        self.raw = data
        self.id = str(data.get("message_id", ""))
        self.message_type = data.get("message_type", "")
        gid = data.get("group_id")
        uid = data.get("user_id")
        self.group_id = str(gid) if gid is not None else ""
        self.user_id = str(uid) if uid is not None else ""
        self.content = data.get("raw_message") or ""

    def __repr__(self):
        return (
            f"OneBotMessage(type={self.message_type}, "
            f"group={self.group_id}, user={self.user_id}, id={self.id})"
        )


class OneBotClient:
    """正向 WebSocket 客户端：接收事件 + 调用 OneBot API。"""

    def __init__(self, ws_url: str, token: str = "", reconnect_delay: float = 5.0,
                 event_queue_size: int = 256):
        if event_queue_size < 1:
            raise ValueError("event_queue_size must be positive")
        self.ws_url = ws_url
        self.token = token
        self.reconnect_delay = reconnect_delay
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._echo_seq = 0
        self._event_queue_size = event_queue_size
        self._event_queue: asyncio.Queue | None = None
        # 事件分发：由上层（main.py）注册
        self.group_msg_handler: MessageHandler | None = None
        self.private_msg_handler: MessageHandler | None = None
        self.on_ready: ReadyHandler | None = None

    async def start(self) -> None:
        """常驻入口：连接成功进入事件循环，异常/断开后自动重连。"""
        while True:
            try:
                await self._run_once()
            except Exception:
                logger.exception("OneBot 连接异常，%.1fs 后重连", self.reconnect_delay)
            await asyncio.sleep(self.reconnect_delay)

    async def _run_once(self) -> None:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        async with websockets.connect(
            self.ws_url, additional_headers=headers, max_size=4 * 1024 * 1024
        ) as ws:
            self._ws = ws
            queue = asyncio.Queue(maxsize=self._event_queue_size)
            self._event_queue = queue
            worker = asyncio.create_task(self._handle_events(queue))
            try:
                logger.info("OneBot 已连接: %s", self.ws_url)
                if self.on_ready is not None:
                    queue.put_nowait((self.on_ready, ()))
                # 只解析事件和完成回执，不能在此等待会调用 API 的业务处理。
                async for raw in ws:
                    await self._dispatch(raw)
            finally:
                if self._ws is ws:
                    self._ws = None
                    self._event_queue = None
                for future in self._pending.values():
                    if not future.done():
                        future.set_result(None)
                self._pending.clear()
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    async def _handle_events(self, queue: asyncio.Queue) -> None:
        """保持业务事件顺序，接收循环同时可继续处理 API 回执。"""
        while True:
            handler, args = await queue.get()
            try:
                await handler(*args)
            except Exception:
                logger.exception("OneBot 事件处理失败")
            finally:
                queue.task_done()

    def _enqueue_event(self, handler, *args) -> None:
        queue = self._event_queue
        if queue is None:
            return
        try:
            queue.put_nowait((handler, args))
        except asyncio.QueueFull:
            # 等待队列空位同样会阻塞回执。显式断开并记录过载，不无限建任务。
            raise RuntimeError("OneBot event queue is full") from None

    async def _dispatch(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("收到非 JSON 数据，忽略")
            return

        if not isinstance(data, dict):
            logger.warning("收到非对象 JSON，忽略")
            return

        if "echo" in data:
            # API 动作回执
            echo = data.get("echo")
            fut = self._pending.pop(echo, None) if isinstance(echo, str) else None
            if fut is not None and not fut.done():
                fut.set_result(data)
            return

        post_type = data.get("post_type")
        if post_type == "meta_event":
            return  # 心跳/生命周期，无需处理
        if post_type != "message":
            return

        msg = OneBotMessage(data)
        if msg.message_type == "group" and self.group_msg_handler is not None:
            self._enqueue_event(self.group_msg_handler, msg)
        elif msg.message_type == "private" and self.private_msg_handler is not None:
            self._enqueue_event(self.private_msg_handler, msg)

    async def call_api(self, action: str, **params) -> dict | None:
        """调用 OneBot API，返回响应 dict；连接断开或超时返回 None。"""
        ws = self._ws
        if ws is None:
            return None
        self._echo_seq += 1
        echo = f"m{self._echo_seq}"
        fut = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        try:
            await ws.send(
                json.dumps({"action": action, "params": params, "echo": echo})
            )
            return await asyncio.wait_for(fut, timeout=10)
        except Exception:
            return None
        finally:
            self._pending.pop(echo, None)
            if not fut.done():
                fut.cancel()

    async def send_group_msg(self, group_id: str, text: str) -> bool:
        """向群发送文本消息；仅 OneBot 返回成功才确认。"""
        if not text:
            return False
        resp = await self.call_api(
            "send_group_msg",
            group_id=int(group_id) if group_id.isdigit() else group_id,
            message=text,
        )
        if resp is None or resp.get("status") != "ok" or resp.get("retcode") not in (0, None):
            logger.warning("群消息发送失败: %s", resp)
            return False
        return True

    async def send_private_msg(self, user_id: str, text: str) -> bool:
        """向用户发送私聊消息。"""
        if not text:
            return False
        resp = await self.call_api(
            "send_private_msg",
            user_id=int(user_id) if user_id.isdigit() else user_id,
            message=text,
        )
        if resp is None or resp.get("status") != "ok" or resp.get("retcode") not in (0, None):
            logger.warning("私聊消息发送失败: %s", resp)
            return False
        return True
