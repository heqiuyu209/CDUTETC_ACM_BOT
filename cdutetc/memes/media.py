"""取图与图床层：CQ 码清理、图片引用解析、下载，以及公网地址拼接。"""

from __future__ import annotations

import logging
import re
from urllib.parse import quote

from cdutetc import config

logger = logging.getLogger("memes")

_CQ_RE = re.compile(r"\[CQ:[^\]]*\]")


def strip_cq_codes(text: str) -> str:
    """去掉 CQ 码并压缩空白。

    清理用户粘贴的旧消息标记，避免污染分类上下文；不会据此下载图片。
    """
    return " ".join(_CQ_RE.sub(" ", str(text or "")).split())


_URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")


def _read_field(item, name: str, default=None):
    """兼容原始事件字典与 botpy SDK 对象两种取值方式。"""
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def normalize_image_url(url: str) -> str:
    """补齐图片地址协议：官方附件常给 ``//host/...`` 或缺协议的裸域名。

    没有协议头的地址 httpx 无法请求，这里统一补成 https；纯文件名原样返回。
    """
    target = str(url or "").strip()
    if not target:
        return ""
    if target.startswith("//"):
        return "https:" + target
    if _URL_SCHEME_RE.match(target):
        return target
    head = target.split("/", 1)[0]
    # 形如 multimedia.nt.qq.com.cn/xxx：第一段是域名且后面带路径，才补协议，
    # 避免把本地文件名（a.jpg）误当域名。
    if "/" in target and "." in head:
        return "https://" + target
    return target


def _attachment_refs(items) -> list[dict]:
    """把附件列表转成 ``[{"url", "file"}]``；非图片附件（content_type 非 image/*）跳过。"""
    refs: list[dict] = []
    if not isinstance(items, (list, tuple)):
        return refs
    for item in items:
        content_type = str(_read_field(item, "content_type", "") or "").lower()
        if content_type and not content_type.startswith("image/"):
            continue
        url = normalize_image_url(_read_field(item, "url", "") or "")
        name = str(
            _read_field(item, "filename", "") or _read_field(item, "file", "") or ""
        )
        if url or name:
            refs.append({"url": url, "file": name})
    return refs


def extract_image_refs(message) -> list[dict]:
    """从群消息里抠出图片引用：读取官方附件及 SDK 原始事件兜底。

    返回 ``[{"url": ..., "file": ...}]``，两者都可能为空，取到哪个用哪个。
    """
    refs: list[dict] = []
    raw = getattr(message, "raw", None)
    if isinstance(raw, dict):
        refs.extend(_attachment_refs(raw.get("attachments")))
    if not refs:
        # 兜底：QQBotMessage 未把 attachments 同步进 raw 时，直接读原始事件对象
        source = getattr(message, "source", None)
        if source is not None:
            refs.extend(_attachment_refs(_read_field(source, "attachments", None)))
    return refs


async def fetch_image_bytes(url: str, *, timeout: float = 10.0) -> bytes:
    """下载图片内容；地址为空或下载失败返回空 bytes，调用方据此跳过。"""
    target = str(url or "").strip()
    if not target:
        return b""
    try:
        import httpx
    except ImportError:  # pragma: no cover - requirements 已固定 httpx
        return b""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(target)
            response.raise_for_status()
            return response.content
    except Exception as exc:  # noqa: BLE001 - 网络问题不能让收图拖垮主流程
        logger.info("收图下载失败：%s（%s）", target[:80], exc)
        return b""
def public_url(relative_path: str, *, base_url: str | None = None) -> str:
    """拼出公网可访问的图片地址；BASE_URL 未配置时返回空串，绝不发半截链接。"""
    base = (config.MEME_BASE_URL if base_url is None else base_url) or ""
    base = base.strip().rstrip("/")
    relative = str(relative_path or "").strip().lstrip("/")
    if not base or not relative:
        return ""
    return f"{base}/{quote(relative, safe='/')}"
