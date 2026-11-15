"""接入客户端统一契约（P2）。

业务层判断协议差异只允许读本模块的能力位：
  - ``supports_passive_reply``  被动回复（复用入站消息 id + 递增 msg_seq）是否可用
  - ``supports_numeric_group_id`` 群标识是否为数字 QQ 群号（官方开放平台是不透明 openid）
  - ``supports_markdown``       主动群消息是否支持 markdown 消息体

禁止再出现 ``config.BOT_PROTOCOL == "qqbot"`` 之类的分支，也禁止用
``gid.isdecimal()`` 之类的字符串启发式猜测接入协议——那是客户端自身的能力。

能力位的推导集中在 :func:`capabilities_for`：显式传入的客户端以自身声明为准，
没有客户端可注入的只读业务函数走 :func:`active_capabilities`，由协议名推导，
因此不存在跨测试/跨进程泄漏的全局可变状态。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from cdutetc.errors import DeliveryResult


class BaseBotClient(ABC):
    """发信契约：能力位 + 结构化发信方法。

    能力位由具体客户端声明，业务层只读不写；``protocol`` 仅用于数据库记录键
    与日志，不允许作为分支依据。
    """

    #: 协议标识（写入数据库 / 日志用，不参与业务分支）
    protocol: str = ""

    #: 是否支持被动回复
    supports_passive_reply: bool = False

    #: 群标识是否为数字 QQ 群号
    supports_numeric_group_id: bool = True

    #: 主动群消息是否支持 markdown
    supports_markdown: bool = False

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

        OneBot 用数字群号，任意标识都收；官方 openid 是不透明串，短纯数字几乎
        一定是被误填的 QQ 群号。这一形态判定属客户端能力，业务层只问结论。
        """
        gid = str(group_id or "").strip()
        if not gid:
            return False
        if self.supports_numeric_group_id:
            return True
        return not (gid.isdecimal() and len(gid) <= self.QQ_NUMERIC_MAX_LEN)


#: 各接入协议的固有能力。新增协议只需在此登记一次，业务层自动适配。
_PROTOCOL_CAPABILITIES: dict[str, Capabilities] = {
    # 官方 QQ 开放平台：openid 非数字、被动回复 + markdown 可用、无主动群消息权限
    "qqbot": Capabilities("qqbot", passive_reply=True,
                          numeric_group_id=False, markdown=True),
    # OneBot / NapCat：数字 QQ 群号、纯文本、主动发送
    "onebot": Capabilities("onebot", passive_reply=False,
                           numeric_group_id=True, markdown=False),
}


def _current_protocol() -> str:
    """读取配置中的接入协议名（本模块是协议配置的唯一消费者）。"""
    from cdutetc import config

    return str(getattr(config, "BOT_PROTOCOL", "onebot") or "onebot")


def capabilities_for(protocol: str | None = None) -> Capabilities:
    """按协议名取能力位；未知协议按 OneBot 语义保守兜底。"""
    name = str(protocol or _current_protocol())
    known = _PROTOCOL_CAPABILITIES.get(name)
    if known is not None:
        return known
    return _PROTOCOL_CAPABILITIES["onebot"]


def _flag(client, name: str, fallback: bool) -> bool:
    """只采信明确的布尔声明；Mock 属性、缺失字段一律回退到协议固有能力。"""
    value = getattr(client, name, None)
    return value if isinstance(value, bool) else fallback


def capabilities_of(client) -> Capabilities:
    """优先采信客户端自报能力位，缺失/非法字段回退到协议固有能力。"""
    if client is None:
        return capabilities_for()
    declared = getattr(client, "protocol", "")
    protocol = declared if isinstance(declared, str) and declared else _current_protocol()
    defaults = capabilities_for(protocol)
    return Capabilities(
        protocol,
        passive_reply=_flag(client, "supports_passive_reply",
                            defaults.supports_passive_reply),
        numeric_group_id=_flag(client, "supports_numeric_group_id",
                               defaults.supports_numeric_group_id),
        markdown=_flag(client, "supports_markdown", defaults.supports_markdown),
    )


def active_capabilities(client=None) -> Capabilities:
    """无客户端注入时的能力位入口（按当前配置协议推导，无全局状态）。"""
    return capabilities_of(client)


def active_protocol(client=None) -> str:
    """接入协议标识，仅用于数据库记录键与日志，不用于分支判断。"""
    capabilities = capabilities_of(client)
    return capabilities.protocol or _current_protocol()
