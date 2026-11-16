"""自动收图编排层：下载 → 准入 → 去重 → 归类 → 落盘 → 记账 → 淘汰。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path

import cdutetc.memes as _facade
from cdutetc import database
from cdutetc.memes.rules import (
    COLLECT_CONTEXT_LINES,
    GIF_SUFFIX,
)

logger = logging.getLogger("memes")

async def collect_one_image(ref: dict, *, group_id: str, text: str = "",
                            context: Iterable[str] = (), root: Path | None = None,
                            downloader: Callable[[str], Awaitable[bytes]] | None = None,
                            user_id: str = "",
                            ) -> dict | None:
    """处理单张图：任何一步不合格就返回 None，绝不让异常冒到主流程。

    链路：配额 → 下载 → MD5 去重 → 尺寸/体积准入 → OCR 截图拦截 → 相似图去重
          → 归类 → 瘦身落盘 → 记账 → 分类 FIFO 淘汰。
    返回 ``{"md5", "category", "tag", "relative_path"}`` 供日志/回执使用。
    """
    url = str((ref or {}).get("url") or "")
    name = str((ref or {}).get("file") or "")
    fetch = downloader or _facade.fetch_image_bytes
    # 单人限流放最前面：额度用完连下载都省了，刷屏的人拿不到任何额外开销。
    if _facade._collect_quota_left(group_id, user_id) <= 0:
        logger.info("收图跳过（单人限流）：%s/%s", group_id, user_id)
        return None
    data = b""
    if url:
        try:
            data = await fetch(url)
        except Exception as exc:  # noqa: BLE001 - 下载失败只跳过这一张
            logger.info("收图下载异常：%s", exc)
            return None
    if not data:
        return None
    md5 = _facade.image_md5(data)
    try:
        if database.meme_hash_exists(md5):
            # 含 deleted=1 的黑名单：删过的图不再回收
            return None
    except Exception as exc:  # noqa: BLE001 - 台账读不到时宁可不收
        logger.info("收图去重查询失败：%s", exc)
        return None
    # QQ 下载地址可能只有 fileid，没有扩展名；GIF 必须按内容识别以保留动画。
    suffix = GIF_SUFFIX if data[:6] in (b"GIF87a", b"GIF89a") else _facade.file_suffix(name, url)
    accepted, reason = _facade.should_collect(data, suffix=suffix)
    if not accepted:
        logger.info("收图跳过（%s）：%s", reason, (name or url)[:80])
        return None
    # OCR 是 CPU 阻塞活儿，必须丢线程池：事件循环还要收消息、发回复。
    ocr_text = ""
    try:
        ocr_text = await asyncio.to_thread(_facade.extract_image_text, data)
    except Exception as exc:  # noqa: BLE001 - OCR 崩了按「无文字」继续
        logger.info("收图 OCR 异常，按无文字处理：%s", exc)
        ocr_text = ""
    accepted, reason = _facade.inspect_ocr_text(ocr_text)
    if not accepted:
        logger.info("收图跳过（%s）：%s", reason, (name or url)[:80])
        return None
    # 相似图去重：同一张图被二次压缩/缩放后 MD5 会变，靠 dHash 兜住。
    dhash = _facade.compute_dhash(data)
    if dhash:
        try:
            if _facade.find_similar_meme(dhash):
                logger.info("收图跳过（相似图已收录）：%s", (name or url)[:80])
                return None
        except Exception as exc:  # noqa: BLE001 - 相似查询失败按「宁可不收」处理
            logger.info("收图相似去重失败：%s", exc)
            return None
    # 图内文字比群聊上下文更能说明图的用途，排在最前面参与归类。
    lines = [ocr_text, text, *[str(item) for item in context][:COLLECT_CONTEXT_LINES]]
    category, tag = _facade.classify_collect(*lines)
    # 落盘前瘦身：静态图压成 500px 宽 JPEG，GIF 原样；MD5 仍用原始字节算。
    payload, store_suffix = _facade.store_meme_bytes(data, suffix=suffix)
    relative = _facade.save_meme_file(payload, category=category, md5=md5, tag=tag,
                              suffix=store_suffix, root=root)
    if not relative:
        return None
    try:
        database.save_meme_hash(md5, category, tag or category, relative, dhash)
    except Exception as exc:  # noqa: BLE001 - 记账失败就当没收，避免重复落盘
        logger.warning("收图记账失败：%s", exc)
        _facade.delete_meme_file(relative, root=root)
        return None
    _facade._record_collect(group_id, user_id)
    # 分类超限滚动淘汰：只在本张真正落盘成功后执行，避免白折腾。
    _facade.enforce_category_cap(category, root=root)
    logger.info("收到表情包：%s（%s）", relative, group_id)
    return {"md5": md5, "category": category, "tag": tag, "relative_path": relative}


async def collect_from_message(message, *, group_id: str, text: str = "",
                               context: Iterable[str] = (), root: Path | None = None,
                               downloader: Callable[[str], Awaitable[bytes]] | None = None,
                               enabled_check: Callable[[str], bool] | None = None,
                               ) -> list[dict]:
    """群消息里所有图片的收图入口（异步后台任务调用）。

    开关关闭、没有图片、库未初始化都直接返回空列表，不抛异常。
    """
    group = str(group_id or "").strip()
    if not group:
        return []
    check = enabled_check or database.meme_collect_enabled
    try:
        if not check(group):
            return []
    except Exception as exc:  # noqa: BLE001 - 开关读不到按关闭处理
        logger.info("收图开关读取失败：%s", exc)
        return []
    refs = _facade.extract_image_refs(message)
    if not refs:
        return []
    # 清理正文中粘贴的旧消息标记，避免污染分类上下文。
    clean_text = _facade.strip_cq_codes(text)
    clean_context = [item for item in map(_facade.strip_cq_codes, context) if item]
    # 单人限流的键：使用适配层提供的成员 OpenID。
    user = ""
    for field in ("user_id", "member_openid", "user_openid", "author_id"):
        candidate = str(_facade._read_field(message, field, "") or "").strip()
        if candidate:
            user = candidate
            break
    collected: list[dict] = []
    for ref in refs:
        outcome = await collect_one_image(
            ref, group_id=group, text=clean_text, context=clean_context,
            root=root, downloader=downloader, user_id=user,
        )
        if outcome:
            collected.append(outcome)
    return collected


def delete_last_sent(group_id: str, *, engine: _facade.MemeEngine | None = None,
                     root: Path | None = None) -> dict | None:
    """删掉该群最近一次发出的表情包文件，并把 MD5 拉黑。

    机器人发的可能是原始素材（没进台账），此时只删文件、返回空 MD5。
    没有任何发送记录时返回 None，由指令层回执「没图可删」。
    """
    target_engine = engine or _facade.get_engine()
    relative = target_engine.recall_last_sent(group_id)
    if not relative:
        return None
    _facade.delete_meme_file(relative, root=root)
    md5 = ""
    try:
        record = database.find_meme_hash_by_path(relative)
        if record:
            md5 = str(record.get("md5") or "")
            if md5:
                database.mark_meme_hash_deleted(md5, file_path=relative)
    except Exception as exc:  # noqa: BLE001 - 台账不可用也要保证文件已删
        logger.info("删图记账失败：%s", exc)
    target_engine.forget_sent(group_id, relative)
    return {"relative_path": relative, "md5": md5}
