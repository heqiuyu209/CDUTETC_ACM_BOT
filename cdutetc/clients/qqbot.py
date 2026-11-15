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
2. 群 @ 事件直接标记已指向机器人；全量群消息事件根据 mentions.is_you
   判断是否 @ 本机器人，普通群聊仍由业务入口按需忽略。
3. 被动回复需要带 msg_id；主动推送（定时提醒）不带 msg_id，
   受平台主动消息配额限制。

botpy 采用延迟导入：未安装时本模块仍可被导入，只有真正启动才报错，
这样纯 OneBot 部署和单元测试都不受影响。
"""

import asyncio
import logging
import secrets
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from cdutetc.clients.base import BaseBotClient

logger = logging.getLogger("qqbot")

MessageHandler = Callable[["QQBotMessage"], Awaitable[None]]
ReadyHandler = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class GroupActivePermission:
    allowed: bool | None = None
    code: str = "unknown"
    platform_code: int | None = None


def _import_botpy():
    """延迟导入 botpy，未安装时给出明确安装指引。"""
    try:
        import botpy
    except ImportError as error:  # pragma: no cover - 取决于运行环境
        raise RuntimeError(
            "使用官方机器人需要安装 botpy：pip install -r requirements-qqbot.txt"
        ) from error
    return botpy


# 群消息去重窗口：平台可能把同一条 @ 消息同时投递到「群 @ 事件」和
# 「群全量事件」，5 分钟内按 msg_id 只处理一次。
GROUP_MSG_DEDUP_TTL = 300.0
# 去重表容量上限：TTL 之外再加一道内存保护，防止极端刷屏撑爆内存。
GROUP_MSG_DEDUP_LIMIT = 2048


def build_group_intents(botpy):
    """构造订阅 intent：群 @ 消息 + 群全量消息一起收。

    平台侧 GROUP_MESSAGE_CREATE（群全量消息）与 GROUP_AT_MESSAGE_CREATE 同属
    GROUP_AND_C2C_EVENT (1 << 25)，即 botpy 的 public_messages，位掩码无需再加；
    真正的开关是「机器人在平台开启接收所有消息」+ 本模块补的 group_message_create 解析器。
    新版本 SDK 若提供具名 group_messages 开关，则顺带打开（老版本会因未知 kwarg 报错）。
    """
    for kwargs in ({"public_messages": True, "group_messages": True},
                   {"public_messages": True}):
        try:
            intents = botpy.Intents(**kwargs)
        except TypeError:
            continue
        if "group_messages" in kwargs:
            logger.info("已订阅群 @ 消息与群全量消息（GROUP_AND_C2C_EVENT）")
        return intents
    return botpy.Intents(public_messages=True)


class QQBotMessage:
    """与 OneBotMessage 字段对齐的消息封装。"""

    __slots__ = ("raw", "source", "id", "message_type", "group_id", "user_id",
                 "content", "always_addressed", "mentions")

    def __init__(self, data, *, message_type: str, group_id: str = "",
                 user_id: str = "", content: str = "", msg_id: str = "",
                 always_addressed: bool = False, mentions=None):
        # raw 保持「字典」形态，与 OneBotMessage 一致（main 里会调用 raw.get）。
        # 原始 botpy 对象另存到 source，便于排查问题。
        self.raw = {}
        # 官方协议的图片只出现在 attachments 里（正文 content 为空串），
        # 必须同步进 raw，否则 memes.extract_image_refs 拿不到任何图片地址。
        attachments = _normalize_attachments(data)
        if attachments:
            self.raw["attachments"] = attachments
        self.source = data
        self.id = str(msg_id or "")
        self.message_type = message_type
        self.group_id = str(group_id or "")
        self.user_id = str(user_id or "")
        self.content = content or ""
        # 官方机器的群消息本身就是 @ 消息，无需再解析 CQ 码
        self.always_addressed = always_addressed
        # 统一 @ 载体：把平台 mentions 规范成 [{"id": ..., "is_you": bool}]，
        # 业务层只读本字段判定是否 @ 到本机器人（官方协议下 CQ 码不存在）。
        self.mentions = [_normalize_mention(item) for item in (mentions or [])]
        if always_addressed and not any(item["is_you"] for item in self.mentions):
            # 事件类型本身即「群里 @ 机器人」，平台未下发 is_you 时按命中自身处理。
            self.mentions.append({"id": "", "is_you": True})

    def __repr__(self):
        return (
            f"QQBotMessage(type={self.message_type}, group={self.group_id[:8]}…, "
            f"user={self.user_id[:8]}…, id={self.id})"
        )


def _field(data, name: str, default=None):
    """兼容原始事件字典与 SDK 模型，保留旧 SDK 没有暴露的字段。"""
    return data.get(name, default) if isinstance(data, dict) else getattr(data, name, default)


def _normalize_attachments(data) -> list[dict]:
    """把入站消息的 attachments 规范成 ``[{"url", "filename", "content_type"}]``。

    平台下发的附件可能是 botpy 的 Attachment 对象（属性访问），也可能是原始
    事件字典里的 dict（键访问），两种形态都要兼容；丢弃无地址也无文件名的空附件。
    """
    raw_attachments = _field(data, "attachments", None)
    if not isinstance(raw_attachments, (list, tuple)):
        return []
    normalized: list[dict] = []
    for item in raw_attachments:
        url = str(_field(item, "url", "") or "")
        filename = str(_field(item, "filename", "") or _field(item, "file", "") or "")
        content_type = str(
            _field(item, "content_type", "") or _field(item, "contentType", "") or ""
        )
        if url or filename:
            normalized.append(
                {"url": url, "filename": filename, "content_type": content_type}
            )
    return normalized


def _normalize_mention(item) -> dict:
    """把平台 mention 规范成 {"id", "is_you"}：不同 SDK 版本字段名不一致。"""
    ident = (
        _field(item, "member_openid", "")
        or _field(item, "user_openid", "")
        or _field(item, "id", "")
        or ""
    )
    return {"id": str(ident), "is_you": _field(item, "is_you", False) is True}


def wrap_group_at_message(message) -> QQBotMessage:
    """把 botpy 的 GroupMessage（@机器人）转成统一消息对象。

    字段来源均做容错：不同 botpy 版本对 author 的嵌套略有差异。
    """
    author = _field(message, "author")
    member_openid = (
        _field(author, "member_openid", "")
        or _field(message, "member_openid", "")
    )
    return QQBotMessage(
        message,
        message_type="group",
        group_id=_field(message, "group_openid", "") or "",
        user_id=member_openid or "",
        content=_field(message, "content", "") or "",
        msg_id=_field(message, "id", "") or "",
        always_addressed=True,
        mentions=_field(message, "mentions", []) or [],
    )


def wrap_group_message(message) -> QQBotMessage:
    """全量群事件不能当成群 @ 事件；只信平台明确的 is_you 布尔标记。"""
    mentions = _field(message, "mentions", []) or []
    wrapped = QQBotMessage(
        message,
        message_type="group",
        group_id=_field(message, "group_openid", "") or "",
        user_id=(
            _field(_field(message, "author"), "member_openid", "")
            or _field(message, "member_openid", "")
        ),
        content=_field(message, "content", "") or "",
        msg_id=_field(message, "id", "") or "",
        always_addressed=any(_field(item, "is_you") is True for item in mentions),
        mentions=mentions,
    )
    return wrapped


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


class QQBotClient(BaseBotClient):
    """官方机器人客户端，接口与 OneBotClient 对齐。"""

    protocol = "qqbot"
    # 官方协议靠入站 msg_id + msg_seq 做被动回复。
    supports_passive_reply = True
    # 群标识是平台下发的 group_openid，不是 QQ 群号。
    supports_numeric_group_id = False
    # 主动群消息支持 markdown 消息体。
    supports_markdown = True

    def __init__(self, appid: str, secret: str, idle_timeout: float = 180.0):
        self.appid = appid
        self.secret = secret
        # 与 OneBot 客户端保持同名参数；官方网关自带心跳，这里仅用于告警
        self.idle_timeout = idle_timeout
        self.group_msg_handler: MessageHandler | None = None
        self.private_msg_handler: MessageHandler | None = None
        self.on_ready: ReadyHandler | None = None
        self._client = None
        self._last_rx = time.monotonic()
        # 主动消息不复用 SDK 的默认序号1；随机起点也减少重启后的序号重合。
        self._active_msg_seq = secrets.randbelow(2**31 - 2) + 1
        # 入站消息幂等表：(group_id, msg_id) -> 首次处理时刻；带 TTL 与容量上限。
        self._recent_group_messages: OrderedDict[tuple[str, str], float] = OrderedDict()

    def _seen_recently(self, wrapped: QQBotMessage, now: float) -> bool:
        """同一条群消息 5 分钟内只处理一次；顺带惰性清理过期条目。"""
        # 官方协议一条 @ 消息可能同时触发群 @ 事件与群全量事件，必须按 msg_id 幂等。
        if not (wrapped.group_id and wrapped.id):
            return False
        key = (wrapped.group_id, wrapped.id)
        hit = self._recent_group_messages.get(key)
        if hit is not None and now - hit < GROUP_MSG_DEDUP_TTL:
            return True
        self._recent_group_messages[key] = now
        self._recent_group_messages.move_to_end(key)
        while self._recent_group_messages:
            oldest_at = next(iter(self._recent_group_messages.values()))
            if len(self._recent_group_messages) > GROUP_MSG_DEDUP_LIMIT \
                    or now - oldest_at >= GROUP_MSG_DEDUP_TTL:
                self._recent_group_messages.popitem(last=False)
                continue
            break
        return False

    async def _receive_group_message(self, wrapped: QQBotMessage) -> None:
        self._last_rx = time.monotonic()
        if self.group_msg_handler is None:
            return
        if self._seen_recently(wrapped, time.monotonic()):
            logger.debug("重复群消息已忽略（msg_id=%s）", wrapped.id)
            return
        try:
            await self.group_msg_handler(wrapped)
        except Exception:
            logger.exception("群消息处理失败")

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        """连接官方网关并常驻（WebSocket 出站连接，无需公网 IP）。"""
        botpy = _import_botpy()
        adapter = self

        class _Client(botpy.Client):
            async def _bot_login(self, *args, **kwargs):
                await super()._bot_login(*args, **kwargs)
                # botpy 1.2.1 缺少此解析器。仅为本连接补缺，原生支持时不覆盖。
                parsers = self._connection.parser
                if "group_message_create" not in parsers:
                    def parse_group_message(payload):
                        # 直接保留原始体；旧 SDK 的 GroupMessage 会丢掉 mentions。
                        self.ws_dispatch("group_message_create", payload.get("d", {}))
                    parsers["group_message_create"] = parse_group_message
                    logger.info("已启用 GROUP_MESSAGE_CREATE 事件兼容处理")

            async def on_ready(self):
                logger.info("官方机器人已连接（AppID=%s）", adapter.appid)
                adapter._last_rx = time.monotonic()
                if adapter.on_ready is not None:
                    await adapter.on_ready()

            async def on_group_at_message_create(self, message):
                await adapter._receive_group_message(wrap_group_at_message(message))

            async def on_group_message_create(self, message):
                await adapter._receive_group_message(wrap_group_message(message))

            async def on_c2c_message_create(self, message):
                adapter._last_rx = time.monotonic()
                wrapped = wrap_c2c_message(message)
                if adapter.private_msg_handler is not None:
                    try:
                        await adapter.private_msg_handler(wrapped)
                    except Exception:
                        logger.exception("私聊消息处理失败")

        intents = build_group_intents(botpy)
        self._client = _Client(intents=intents)
        try:
            await self._client.start(appid=self.appid, secret=self.secret)
        finally:
            self._client = None

    # ---------- 发送 ----------

    def _api(self):
        client = self._client
        return getattr(client, "api", None) if client is not None else None

    @staticmethod
    def _message_id(response) -> str:
        """botpy's successful message response is a message-shaped object/dict."""
        if isinstance(response, dict):
            value = response.get("id") or response.get("message_id")
        else:
            value = getattr(response, "id", None)
        return str(value or "").strip()

    async def _post_result(self, action: str, *, mode, **params):
        from cdutetc.errors import (
            DeliveryCode,
            DeliveryResult,
            result_from_exception,
        )

        protocol = "qqbot"
        api = self._api()
        if api is None:
            return DeliveryResult(False, DeliveryCode.NOT_CONNECTED, protocol, mode)
        func = getattr(api, action, None)
        if func is None:
            return DeliveryResult(False, DeliveryCode.UNSUPPORTED, protocol, mode,
                                  detail="sdk_method_missing")
        try:
            response = await func(**params)
        except Exception as error:
            result = result_from_exception(error, protocol=protocol, mode=mode)
            logger.warning("%s send failed (%s, platform_code=%s)", action, result.code.value, result.platform_code)
            return result
        message_id = self._message_id(response)
        if response is None:
            return DeliveryResult(False, DeliveryCode.TIMEOUT_UNKNOWN, protocol, mode,
                                  detail="no_response")
        if not message_id:
            return DeliveryResult(False, DeliveryCode.UNKNOWN, protocol, mode,
                                  detail="missing_message_id")
        return DeliveryResult.sent(protocol, mode, message_id)

    async def get_group_active_permission(self, group_id: str) -> GroupActivePermission:
        """读取平台记录的本群主动发言开关；无白名单/缺字段时保持未知。"""
        from cdutetc.errors import DeliveryMode, result_from_exception

        if not str(group_id or "").strip():
            return GroupActivePermission(code="invalid_target")
        api = self._api()
        if api is None:
            return GroupActivePermission(code="not_connected")
        http = getattr(api, "_http", None)
        if not callable(getattr(http, "request", None)):
            return GroupActivePermission(code="unsupported")
        try:
            # 复用已连接SDK的鉴权，兼容尚未封装bot_state方法的botpy版本。
            from botpy.http import Route
        except ImportError:
            return GroupActivePermission(code="unsupported")
        try:
            route = Route("GET", "/v2/groups/{group_openid}/bot_state", group_openid=group_id)
            response = await asyncio.wait_for(http.request(route), timeout=5.0)
        except Exception as error:
            failure = result_from_exception(error, protocol="qqbot", mode=DeliveryMode.ACTIVE)
            return GroupActivePermission(code=failure.code.value, platform_code=failure.platform_code)
        if isinstance(response, dict):
            value = response.get("allow_proactive_msg")
            if type(value) is bool:
                return GroupActivePermission(allowed=value, code="ok")
            code = response.get("code") or response.get("err_code")
            if type(code) is int and code > 0:
                return GroupActivePermission(code="platform_error", platform_code=code)
        return GroupActivePermission()

    async def upload_group_media(self, group_id: str, url: str, *,
                                 file_type: int = 1) -> tuple[str, int] | None:
        """把公网图片 URL 换成 file_info（官方富媒体发图的前置步骤）。

        Returns:
            (file_info, ttl 秒)；任何前置条件不满足或平台报错都返回 None，
            由调用方静默降级，绝不让发图失败影响主业务流程。
        """
        group = str(group_id or "").strip()
        address = str(url or "").strip()
        if not group or not address:
            return None
        api = self._api()
        if api is None:
            return None
        http = getattr(api, "_http", None)
        if not callable(getattr(http, "request", None)):
            return None
        try:
            from botpy.http import Route
        except ImportError:
            return None
        route = Route("POST", "/v2/groups/{group_openid}/files", group_openid=group)
        # sdkfileid 与 url 二选一，这里只走公网 URL 拉取。
        payload = {"file_type": int(file_type), "url": address, "sdkfileid": ""}
        try:
            response = await asyncio.wait_for(http.request(route, json=payload), timeout=10.0)
        except Exception as error:
            logger.warning("表情包上传失败，跳过本次发图: %s", error)
            return None
        if not isinstance(response, dict):
            return None
        file_info = str(response.get("file_info") or "").strip()
        if not file_info:
            logger.warning("表情包上传未返回 file_info，跳过本次发图")
            return None
        ttl = response.get("ttl")
        try:
            ttl_seconds = int(ttl) if ttl is not None else 0
        except (TypeError, ValueError):
            ttl_seconds = 0
        return file_info, ttl_seconds

    async def send_group_media_result(self, group_id: str, file_info: str, *,
                                      reply_to: str | None = None, reply_seq: int = 1):
        """用 msg_type=7 发富媒体（图片）；有 reply_to 时走被动回复。"""
        from cdutetc.errors import DeliveryCode, DeliveryMode, DeliveryResult

        mode = DeliveryMode.PASSIVE if reply_to is not None else DeliveryMode.ACTIVE
        if not str(file_info or "").strip():
            return DeliveryResult(False, DeliveryCode.EMPTY_MESSAGE, "qqbot", mode)
        if not str(group_id or "").strip():
            return DeliveryResult(False, DeliveryCode.INVALID_TARGET, "qqbot", mode,
                                  detail="empty_group_id")
        if reply_to is not None and (not reply_to or type(reply_seq) is not int
                                     or not 1 <= reply_seq <= 5):
            return DeliveryResult(False, DeliveryCode.INVALID_REQUEST, "qqbot", mode,
                                  detail="invalid_passive_reply")
        params = {
            "group_openid": group_id,
            "msg_type": 7,
            "media": {"file_info": file_info},
        }
        if reply_to is not None:
            params["msg_id"] = reply_to
            params["msg_seq"] = reply_seq
        else:
            self._active_msg_seq = self._active_msg_seq % (2**31 - 1) + 1
            params["msg_seq"] = self._active_msg_seq
        return await self._post_result("post_group_message", mode=mode, **params)

    async def send_group_msg_result(self, group_id: str, text: str, *,
                                    reply_to: str | None = None, reply_seq: int = 1,
                                    message_format: str = "text"):
        """主动发送默认不带 msg_id；被动回复必须显式提供 reply_to。"""
        from cdutetc.errors import DeliveryCode, DeliveryMode, DeliveryResult

        mode = DeliveryMode.PASSIVE if reply_to is not None else DeliveryMode.ACTIVE
        if not text:
            return DeliveryResult(False, DeliveryCode.EMPTY_MESSAGE, "qqbot", mode)
        if not str(group_id or "").strip():
            return DeliveryResult(False, DeliveryCode.INVALID_TARGET, "qqbot", mode,
                                  detail="empty_group_id")
        if message_format not in ("text", "markdown"):
            return DeliveryResult(False, DeliveryCode.INVALID_REQUEST, "qqbot", mode,
                                  detail="invalid_message_format")
        if reply_to is not None and (not reply_to or type(reply_seq) is not int
                                     or not 1 <= reply_seq <= 5):
            return DeliveryResult(False, DeliveryCode.INVALID_REQUEST, "qqbot", mode,
                                  detail="invalid_passive_reply")
        params = {"group_openid": group_id}
        if message_format == "markdown":
            params.update(msg_type=2, markdown={"content": text})
        else:
            params.update(msg_type=0, content=text)
        if reply_to is not None:
            params["msg_id"] = reply_to
            params["msg_seq"] = reply_seq
        else:
            self._active_msg_seq = self._active_msg_seq % (2**31 - 1) + 1
            params["msg_seq"] = self._active_msg_seq
        return await self._post_result("post_group_message", mode=mode, **params)

    async def send_group_msg(self, group_id: str, text: str) -> bool:
        """Backward-compatible active send; never borrows a received msg_id."""
        return bool(await self.send_group_msg_result(group_id, text))

    async def send_private_msg_result(self, user_id: str, text: str, *,
                                      reply_to: str | None = None, reply_seq: int = 1):
        """Active private send by default; passive reply only with explicit reply_to."""
        from cdutetc.errors import DeliveryCode, DeliveryMode, DeliveryResult

        mode = DeliveryMode.PASSIVE if reply_to is not None else DeliveryMode.ACTIVE
        if not text:
            return DeliveryResult(False, DeliveryCode.EMPTY_MESSAGE, "qqbot", mode)
        if not str(user_id or "").strip():
            return DeliveryResult(False, DeliveryCode.INVALID_TARGET, "qqbot", mode,
                                  detail="empty_user_id")
        if reply_to is not None and (not reply_to or type(reply_seq) is not int
                                     or not 1 <= reply_seq <= 4):
            return DeliveryResult(False, DeliveryCode.INVALID_REQUEST, "qqbot", mode,
                                  detail="invalid_passive_reply")
        params = {"openid": user_id, "msg_type": 0, "content": text}
        if reply_to is not None:
            params["msg_id"] = reply_to
            params["msg_seq"] = reply_seq
        return await self._post_result("post_c2c_message", mode=mode, **params)

    async def send_private_msg(self, user_id: str, text: str) -> bool:
        """Backward-compatible active send; never borrows a received msg_id."""
        return bool(await self.send_private_msg_result(user_id, text))
