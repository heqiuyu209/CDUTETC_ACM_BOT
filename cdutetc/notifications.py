"""Protocol-neutral notification entry point.

Main code can call send_group/send_private and consume DeliveryResult; the active
or passive mode is explicit. No passive-to-active fallback is performed.

协议差异一律读 client 的能力位（见 ``clients/base.py``），本模块不再判断
``config.BOT_PROTOCOL``。
"""

from __future__ import annotations

import asyncio

from cdutetc.clients.base import capabilities_of
from cdutetc.errors import DeliveryCode, DeliveryMode, DeliveryResult

# 底层网络I/O必须有上界，否则APScheduler调度器会被永久挂起（max_instances=1）。
SEND_TIMEOUT_SECONDS = 10.0


def _protocol(client) -> str:
    """协议标识只作记录键；能力差异一律走 client 能力位。"""
    return capabilities_of(client).protocol


def _unsupported(client, mode: DeliveryMode) -> DeliveryResult:
    return DeliveryResult(False, DeliveryCode.UNSUPPORTED, _protocol(client), mode)


def _timeout_result(client, mode: DeliveryMode) -> DeliveryResult:
    return DeliveryResult(False, DeliveryCode.NETWORK_ERROR, _protocol(client), mode,
                          detail="发信超时(10s)")


async def _send_with_timeout(client, send_call, mode: DeliveryMode) -> DeliveryResult:
    """统一包裹底层发信协程：超时转 NETWORK_ERROR，绝不让调用方永久挂起。"""
    try:
        return await asyncio.wait_for(send_call, timeout=SEND_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return _timeout_result(client, mode)


async def send_group(client, group_id: str, text: str, *, reply_to: str | None = None,
                     reply_seq: int = 1, message_format: str = "text") -> DeliveryResult:
    """Send actively by default; supply reply_to only for a deliberate reply."""
    detailed = getattr(client, "send_group_msg_result", None)
    mode = DeliveryMode.PASSIVE if reply_to is not None else DeliveryMode.ACTIVE
    if reply_to == "":
        return DeliveryResult(False, DeliveryCode.INVALID_REQUEST, _protocol(client), mode,
                              detail="empty_reply_to")
    if not callable(detailed):
        return _unsupported(client, mode)
    if message_format not in ("text", "markdown"):
        return DeliveryResult(False, DeliveryCode.INVALID_REQUEST, _protocol(client), mode,
                              detail="invalid_message_format")
    if message_format == "markdown":
        # markdown 是官方群通道的能力，OneBot 纯文本不提供该通道。
        if not capabilities_of(client).supports_markdown:
            return _unsupported(client, mode)
        return await _send_with_timeout(
            client,
            detailed(group_id, text, reply_to=reply_to, reply_seq=reply_seq,
                     message_format=message_format),
            mode,
        )
    if reply_to and capabilities_of(client).supports_passive_reply:
        return await _send_with_timeout(
            client, detailed(group_id, text, reply_to=reply_to, reply_seq=reply_seq), mode)
    return await _send_with_timeout(client, detailed(group_id, text, reply_to=reply_to), mode)


async def send_private(client, user_id: str, text: str, *, reply_to: str | None = None,
                       reply_seq: int = 1) -> DeliveryResult:
    """Send actively by default; supply reply_to only for a deliberate reply."""
    detailed = getattr(client, "send_private_msg_result", None)
    mode = DeliveryMode.PASSIVE if reply_to is not None else DeliveryMode.ACTIVE
    if reply_to == "":
        return DeliveryResult(False, DeliveryCode.INVALID_REQUEST, _protocol(client), mode,
                              detail="empty_reply_to")
    if not callable(detailed):
        return _unsupported(client, mode)
    if reply_to and capabilities_of(client).supports_passive_reply:
        return await _send_with_timeout(
            client, detailed(user_id, text, reply_to=reply_to, reply_seq=reply_seq), mode)
    return await _send_with_timeout(client, detailed(user_id, text, reply_to=reply_to), mode)
