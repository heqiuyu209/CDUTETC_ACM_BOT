"""官方客户端契约与能力默认值，保留既有 qqbot 数据库记录标识。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from cdutetc.errors import DeliveryResult


class BaseBotClient(ABC):
    """发信契约：能力位 + 结构化发信方法。

    能力位由具体客户端声明，业务层只读不写；``protocol`` 仅用于数据库记录键
    与日志，不允许作为分支依据。
    """

    #: 协议标识（写入数据库 / 日志用，不参与业务分支）
    protocol: str = "qqbot"

    #: 是否支持被动回复
    supports_passive_reply: bool = True

    #: 群标识是否为数字 QQ 群号
    supports_numeric_group_id: bool = False

    #: 主动群消息是否支持 markdown
    supports_markdown: bool = True

    @abstractmethod
    async def send_group_msg_result(self, group_id: str, text: str, *,
                                    reply_to: str | None = None,
                                    reply_seq: int = 1) -> DeliveryResult:
        """发送群消息，返回结构化结果；不抛底层异常。"""

    def accepts_group_id(self, group_id: str) -> bool:
        """该群标识是否可作为授权目标；形态判定归客户端，业务层不猜字符串。"""
        return capabilities_of(self).accepts_group_id(group_id)

    @abstractmethod
    async def send_private_msg_result(self, user_id: str, text: str, *,
                                      reply_to: str | None = None,
                                      reply_seq: int = 1) -> DeliveryResult:
        """发送私聊消息，返回结构化结果；不抛底层异常。"""


class Capabilities:
    """纯能力位载体：给拿不到客户端实例的只读业务函数使用。"""

    __slots__ = ("protocol", "supports_passive_reply",
                 "supports_numeric_group_id", "supports_markdown")

    #: QQ 群号的长度上限（超过即视为官方不透明 openid）
    QQ_NUMERIC_MAX_LEN = 12

    def __init__(self, protocol: str, *, passive_reply: bool,
                 numeric_group_id: bool, markdown: bool) -> None:
        self.protocol = protocol
        self.supports_passive_reply = passive_reply
        self.supports_numeric_group_id = numeric_group_id
        self.supports_markdown = markdown

    def accepts_group_id(self, group_id: str) -> bool:
        """群标识是否可用于授权。

        官方 OpenID 是不透明串，短纯数字几乎一定是被误填的 QQ 群号。
        """
        gid = str(group_id or "").strip()
        if not gid:
            return False
        if self.supports_numeric_group_id:
            return True
        return not (gid.isdecimal() and len(gid) <= self.QQ_NUMERIC_MAX_LEN)


def capabilities_for() -> Capabilities:
    """所有入口均使用官方能力，沿用 qqbot 通知状态命名空间。"""
    return Capabilities("qqbot", passive_reply=True, numeric_group_id=False, markdown=True)


def _flag(client, name: str, fallback: bool) -> bool:
    """只采信明确的布尔声明；Mock 属性、缺失字段一律回退到协议固有能力。"""
    value = getattr(client, name, None)
    return value if isinstance(value, bool) else fallback


def capabilities_of(client) -> Capabilities:
    """优先采信客户端自报能力位，缺失/非法字段回退到协议固有能力。"""
    if client is None:
        return capabilities_for()
    protocol = "qqbot"
    defaults = capabilities_for()
    return Capabilities(
        protocol,
        passive_reply=_flag(client, "supports_passive_reply",
                            defaults.supports_passive_reply),
        numeric_group_id=False,
        markdown=_flag(client, "supports_markdown", defaults.supports_markdown),
    )


def active_capabilities(client=None) -> Capabilities:
    """无客户端注入时的能力位入口（固定使用官方默认值，无全局状态）。"""
    return capabilities_of(client)


def active_protocol(client=None) -> str:
    """接入协议标识，仅用于数据库记录键与日志，不用于分支判断。"""
    capabilities = capabilities_of(client)
    return capabilities.protocol
