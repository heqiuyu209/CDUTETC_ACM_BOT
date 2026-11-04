"""QQ 开放平台官方机器人适配层：把 botpy 事件包装成与 OneBotClient 相同的接口。

设计目标：让 main.py 的业务逻辑一行都不用改，只换接入层。
对外暴露的成员与 OneBotClient 完全一致：
    start() / send_group_msg() / send_private_msg()
    group_msg_handler / private_msg_handler / on_ready
消息对象同样提供 group_id / user_id / content / raw / id 字段。

与 OneBot 的关键差异（务必了解）：
1. 官方机器人拿不到 QQ 号，只有 group_openid / member_openid。
   因此 ACM_ALLOWED_GROUPS / ACM_TEACHER_WHITELIST / ACM_SUPER_ADMIN
   里填的必须是 openid，不能填 QQ 号。
2. 群消息只在「@机器人」时推送（on_group_at_message_create），
   消息本身就已指向机器人，所以消息对象带 always_addressed=True。
3. 被动回复需要带 msg_id；主动推送（定时提醒）不带 msg_id，
   受平台主动消息配额限制。

botpy 采用延迟导入：未安装时本模块仍可被导入，只有真正启动才报错，
这样纯 OneBot 部署和单元测试都不受影响。
"""

import asyncio
import logging
import time
from typing import Awaitable, Callable

logger = logging.getLogger("qqbot")

MessageHandler = Callable[["QQBotMessage"], Awaitable[None]]
ReadyHandler = Callable[[], Awaitable[None]]


def _import_botpy():
    """延迟导入 botpy，未安装时给出明确安装指引。"""
    try:
        import botpy  # noqa: F401
    except ImportError as error:  # pragma: no cover - 取决于运行环境
        raise RuntimeError(
            "使用官方机器人需要安装 botpy：pip install -r requirements-qqbot.txt"
        ) from error
    return botpy


class QQBotMessage:
    """与 OneBotMessage 字段对齐的消息封装。"""

    __slots__ = ("raw", "source", "id", "message_type", "group_id", "user_id",
                 "content", "always_addressed")

    def __init__(self, data, *, message_type: str, group_id: str = "",
                 user_id: str = "", content: str = "", msg_id: str = "",
                 always_addressed: bool = False):
        # raw 保持「字典」形态，与 OneBotMessage 一致（main 里会调用 raw.get）。
        # 原始 botpy 对象另存到 source，便于排查问题。
        self.raw = {}
        self.source = data
        self.id = str(msg_id or "")
        self.message_type = message_type
        self.group_id = str(group_id or "")
        self.user_id = str(user_id or "")
        self.content = content or ""
        # 官方机器的群消息本身就是 @ 消息，无需再解析 CQ 码
        self.always_addressed = always_addressed

    def __repr__(self):
        return (
            f"QQBotMessage(type={self.message_type}, group={self.group_id[:8]}…, "
            f"user={self.user_id[:8]}…, id={self.id})"
        )


def wrap_group_at_message(message) -> QQBotMessage:
    """把 botpy 的 GroupMessage（@机器人）转成统一消息对象。

    字段来源均做容错：不同 botpy 版本对 author 的嵌套略有差异。
    """
    author = getattr(message, "author", None)
    member_openid = (
        getattr(author, "member_openid", "")
        or getattr(message, "member_openid", "")
    )
    return QQBotMessage(
        message,
        message_type="group",
        group_id=getattr(message, "group_openid", "") or "",
        user_id=member_openid or "",
        content=getattr(message, "content", "") or "",
        msg_id=getattr(message, "id", "") or "",
        always_addressed=True,
    )


def wrap_c2c_message(message) -> QQBotMessage:
    """把 botpy 的 C2CMessage（私聊）转成统一消息对象。"""
    author = getattr(message, "author", None)
    user_openid = (
        getattr(author, "user_openid", "")
        or getattr(message, "user_openid", "")
    )
    return QQBotMessage(
        message,
        message_type="private",
        group_id="",
        user_id=user_openid or "",
        content=getattr(message, "content", "") or "",
        msg_id=getattr(message, "id", "") or "",
        always_addressed=True,
    )


class QQBotClient:
    """官方机器人客户端，接口与 OneBotClient 对齐。"""

    def __init__(self, appid: str, secret: str, idle_timeout: float = 180.0):
        self.appid = appid
        self.secret = secret
        # 与 OneBot 客户端保持同名参数；官方网关自带心跳，这里仅用于告警
        self.idle_timeout = idle_timeout
        self.group_msg_handler: MessageHandler | None = None
        self.private_msg_handler: MessageHandler | None = None
        self.on_ready: ReadyHandler | None = None
        self._client = None
        # 每个群最近一次收到的消息 id，用于被动回复
        self._last_msg_id: dict[str, str] = {}
        self._last_rx = time.monotonic()

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        """连接官方网关并常驻（WebSocket 出站连接，无需公网 IP）。"""
        botpy = _import_botpy()
        adapter = self

        class _Client(botpy.Client):
            async def on_ready(self):
                logger.info("官方机器人已连接（AppID=%s）", adapter.appid)
                adapter._last_rx = time.monotonic()
                if adapter.on_ready is not None:
                    await adapter.on_ready()

            async def on_group_at_message_create(self, message):
                adapter._last_rx = time.monotonic()
                wrapped = wrap_group_at_message(message)
                if wrapped.id:
                    adapter._last_msg_id[wrapped.group_id] = wrapped.id
                if adapter.group_msg_handler is not None:
                    try:
                        await adapter.group_msg_handler(wrapped)
                    except Exception:  # noqa: BLE001
                        logger.exception("群消息处理失败")

            async def on_c2c_message_create(self, message):
                adapter._last_rx = time.monotonic()
                wrapped = wrap_c2c_message(message)
                if adapter.private_msg_handler is not None:
                    try:
                        await adapter.private_msg_handler(wrapped)
                    except Exception:  # noqa: BLE001
                        logger.exception("私聊消息处理失败")

        intents = botpy.Intents(public_messages=True)
        self._client = _Client(intents=intents)
        try:
            await self._client.start(appid=self.appid, secret=self.secret)
        finally:
            self._client = None

    # ---------- 发送 ----------

    def _api(self):
        client = self._client
        return getattr(client, "api", None) if client is not None else None

    async def _post(self, action: str, **params) -> bool:
        """调用官方 API；异常不抛出，返回是否成功，避免拖垮提醒循环。"""
        api = self._api()
        if api is None:
            logger.warning("官方机器人未连接，%s 跳过", action)
            return False
        func = getattr(api, action, None)
        if func is None:  # pragma: no cover - 取决于 botpy 版本
            logger.error("botpy 不支持 %s，请检查 botpy 版本", action)
            return False
        try:
            await func(**params)
            return True
        except Exception as error:  # noqa: BLE001
            logger.warning("%s 发送失败: %s", action, error)
            return False

    async def send_group_msg(self, group_id: str, text: str) -> bool:
        """向群发消息；群内收到过消息时按被动回复发送（不占主动配额）。"""
        if not text:
            return False
        params = {"group_openid": group_id, "msg_type": 0, "content": text}
        msg_id = self._last_msg_id.get(group_id)
        if msg_id:
            params["msg_id"] = msg_id
        ok = await self._post("post_group_message", **params)
        if not ok and "msg_id" in params:
            # 有的版本不接受 msg_id，退化重试一次主动消息
            params.pop("msg_id", None)
            ok = await self._post("post_group_message", **params)
        return ok

    async def send_private_msg(self, user_id: str, text: str) -> bool:
        """向用户发私聊（主动消息，受平台配额限制）。"""
        if not text:
            return False
        return await self._post(
            "post_c2c_message", openid=user_id, msg_type=0, content=text,
        )
