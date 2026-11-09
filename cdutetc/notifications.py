"""Protocol-neutral notification entry point.

Main code can call send_group/send_private and consume DeliveryResult; the active
or passive mode is explicit. No passive-to-active fallback is performed.
"""

from __future__ import annotations

import asyncio

from cdutetc import config
from cdutetc.errors import DeliveryCode, DeliveryMode, DeliveryResult


async def map_by_group(items, handle, *, concurrency: int = 4):
    """不同群有界并发，同群保持顺序；返回值仍与输入逐项对应。"""
    items = list(items)
    groups = {}
    for index, item in enumerate(items):
        groups.setdefault(item["group_id"], []).append((index, item))
    results = [None] * len(items)
    semaphore = asyncio.Semaphore(concurrency)

    async def worker(entries):
        for index, item in entries:
            async with semaphore:
                results[index] = await handle(item)

    tasks = [asyncio.create_task(worker(entries)) for entries in groups.values()]
    try:
        await asyncio.gather(*tasks)
    finally:
        # 扫描任务取消/异常时收回子任务，不能让上一轮在后台继续发送。
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return results


def _unsupported(mode: DeliveryMode) -> DeliveryResult:
    return DeliveryResult(False, DeliveryCode.UNSUPPORTED, config.BOT_PROTOCOL, mode)


async def send_group(client, group_id: str, text: str, *, reply_to: str | None = None,
                     reply_seq: int = 1, message_format: str = "text") -> DeliveryResult:
    """Send actively by default; supply reply_to only for a deliberate reply."""
    detailed = getattr(client, "send_group_msg_result", None)
    mode = DeliveryMode.PASSIVE if reply_to is not None else DeliveryMode.ACTIVE
    if reply_to == "":
        return DeliveryResult(False, DeliveryCode.INVALID_REQUEST, config.BOT_PROTOCOL, mode,
                              detail="empty_reply_to")
    if not callable(detailed):
        return _unsupported(mode)
    if message_format not in ("text", "markdown"):
        return DeliveryResult(False, DeliveryCode.INVALID_REQUEST, config.BOT_PROTOCOL, mode,
                              detail="invalid_message_format")
    if message_format == "markdown":
        if config.BOT_PROTOCOL != "qqbot":
            return _unsupported(mode)
        return await detailed(group_id, text, reply_to=reply_to, reply_seq=reply_seq,
                              message_format=message_format)
    if config.BOT_PROTOCOL == "qqbot" and reply_to:
        return await detailed(group_id, text, reply_to=reply_to, reply_seq=reply_seq)
    return await detailed(group_id, text, reply_to=reply_to)


async def send_private(client, user_id: str, text: str, *, reply_to: str | None = None,
                       reply_seq: int = 1) -> DeliveryResult:
    """Send actively by default; supply reply_to only for a deliberate reply."""
    detailed = getattr(client, "send_private_msg_result", None)
    mode = DeliveryMode.PASSIVE if reply_to is not None else DeliveryMode.ACTIVE
    if reply_to == "":
        return DeliveryResult(False, DeliveryCode.INVALID_REQUEST, config.BOT_PROTOCOL, mode,
                              detail="empty_reply_to")
    if not callable(detailed):
        return _unsupported(mode)
    if config.BOT_PROTOCOL == "qqbot" and reply_to:
        return await detailed(user_id, text, reply_to=reply_to, reply_seq=reply_seq)
    return await detailed(user_id, text, reply_to=reply_to)
