"""入库体检层：OCR 文本判定、尺寸/体积准入与静态图瘦身压缩。"""

from __future__ import annotations

import io
import logging
import re
import threading

from cdutetc.memes.rules import (
    _OCR_CODE_SYMBOLS,
    _OCR_MIN_SCORE,
    COLLECT_JPEG_QUALITY,
    COLLECT_MAX_ASPECT,
    COLLECT_MAX_BYTES,
    COLLECT_MAX_WIDTH,
    COLLECT_MIN_ASPECT,
    COLLECT_MIN_SIDE,
    GIF_SUFFIX,
    OCR_CODE_HINTS,
    OCR_CODE_SYMBOL_MIN,
    OCR_MAX_CHARS,
)

logger = logging.getLogger("memes")

_ocr_engine = None
_ocr_unavailable = False
_ocr_lock = threading.Lock()


def _get_ocr_engine():
    """懒加载 RapidOCR 单例；依赖缺失或初始化失败时返回 None（只尝试一次）。"""
    global _ocr_engine, _ocr_unavailable
    if _ocr_engine is not None or _ocr_unavailable:
        return _ocr_engine
    with _ocr_lock:
        if _ocr_engine is not None or _ocr_unavailable:
            return _ocr_engine
        try:
            from rapidocr_onnxruntime import RapidOCR
        except Exception as exc:  # noqa: BLE001 - 没装/装坏都按不可用处理
            _ocr_unavailable = True
            logger.info("OCR 不可用，收图不做文字判定：%s", exc)
            return None
        try:
            _ocr_engine = RapidOCR()
        except Exception as exc:  # noqa: BLE001 - 模型文件缺失等
            _ocr_unavailable = True
            logger.info("OCR 初始化失败，收图不做文字判定：%s", exc)
            return None
    return _ocr_engine


def extract_image_text(data: bytes) -> str:
    """识别图片里的文字，返回按行拼接的纯文本；不可用或失败时返回空串。

    这是阻塞调用（CPU 推理），在异步链路里必须走 :func:`asyncio.to_thread`。
    """
    if not data:
        return ""
    engine = _get_ocr_engine()
    if engine is None:
        return ""
    try:
        result, _ = engine(data)
    except Exception as exc:  # noqa: BLE001 - 坏图/模型异常都当无文字
        logger.info("OCR 识别失败，按无文字处理：%s", exc)
        return ""
    if not result:
        return ""
    lines: list[str] = []
    for item in result:
        # RapidOCR 每项形如 [box, text, score]，个别版本会少字段。
        try:
            text = str(item[1] or "").strip()
        except (IndexError, TypeError):
            continue
        try:
            score = float(item[2])
        except (IndexError, TypeError, ValueError):
            score = 1.0
        if text and score >= _OCR_MIN_SCORE:
            lines.append(text)
    return "\n".join(lines)


def ocr_text_length(text: str) -> int:
    """图内有效字符数：不计空白，避免整篇排版空白被误当成「字多」。"""
    return len(re.sub(r"\s+", "", str(text or "")))


def looks_like_code_shot(text: str) -> bool:
    """像代码/题面截图吗：命中特征串，或 ; { } 这类符号堆了 3 个以上。"""
    raw = str(text or "")
    lowered = raw.lower()
    for hint in OCR_CODE_HINTS:
        if hint.lower() in lowered:
            return True
    symbols = sum(raw.count(symbol) for symbol in _OCR_CODE_SYMBOLS)
    return symbols >= OCR_CODE_SYMBOL_MIN


def inspect_ocr_text(text: str) -> tuple[bool, str]:
    """图内文字体检，返回（是否收录, 原因）。空文本一律放行。

    先看字数（长截图的典型特征），再看代码/题面特征，命中任一即拒收。
    """
    if ocr_text_length(text) == 0:
        return True, ""
    if ocr_text_length(text) > OCR_MAX_CHARS:
        return False, "图内文字过多，疑似截图"
    if looks_like_code_shot(text):
        return False, "疑似代码或题面截图"
    return True, ""
def inspect_static_image(data: bytes) -> tuple[bool, str]:
    """静态图准入判定，返回（是否收录, 原因）。

    过滤的是「不像表情包」的图：超大原图、小图标、以及宽高比夸张的代码/长截图。
    Pillow 缺失或图片损坏时一律拒收——宁可少收，也不把垃圾写进素材目录。
    """
    if len(data) > COLLECT_MAX_BYTES:
        return False, f"体积 {len(data) // 1024}KB 超过 {COLLECT_MAX_BYTES // 1024}KB"
    try:
        from PIL import Image
    except ImportError:  # 部署环境没装 Pillow：不猜尺寸，直接放弃收录
        return False, "缺少 Pillow"
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
    except Exception:  # noqa: BLE001 - 损坏图/伪装图都走拒收
        return False, "无法解析"
    if not width or not height:
        return False, "尺寸异常"
    if width < COLLECT_MIN_SIDE or height < COLLECT_MIN_SIDE:
        return False, f"尺寸 {width}x{height} 过小"
    aspect = width / height
    if aspect > COLLECT_MAX_ASPECT:
        return False, f"宽高比 {aspect:.2f} 太宽（疑似长截图）"
    if aspect < COLLECT_MIN_ASPECT:
        return False, f"宽高比 {aspect:.2f} 太窄（疑似长截图）"
    return True, ""


def should_collect(data: bytes, *, suffix: str) -> tuple[bool, str]:
    """收图准入：GIF 动图直接放行，静态图走尺寸/比例/体积过滤。"""
    if not data:
        return False, "内容为空"
    if str(suffix or "").lower() == GIF_SUFFIX:
        return True, ""
    return inspect_static_image(data)


def shrink_static_image(data: bytes, *, max_width: int = COLLECT_MAX_WIDTH,
                        quality: int = COLLECT_JPEG_QUALITY) -> bytes:
    """把静态图等比缩到 ``max_width`` 内并转成 JPEG，返回新字节。

    返回 ``data`` 本身表示「没法压」（Pillow 缺失、图损坏、编码失败），
    调用方据此保留原图与原始后缀。动图不走这里，避免多帧动画被压成一帧。
    """
    try:
        from PIL import Image
    except ImportError:  # 部署环境没装 Pillow：原样保存，不影响收录
        return data
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            if image.mode in ("RGBA", "LA", "P"):
                # 透明图统一铺白底：直接 convert("RGB") 会把透明区压成黑色
                rgba = image.convert("RGBA")
                frame = Image.new("RGB", rgba.size, (255, 255, 255))
                frame.paste(rgba, mask=rgba.split()[-1])
            else:
                frame = image.convert("RGB")
    except Exception as exc:  # noqa: BLE001 - 解析失败就按原图存
        logger.info("表情包压缩跳过（无法解析）：%s", exc)
        return data
    if frame.width > max_width:
        ratio = max_width / frame.width
        frame = frame.resize((max_width, max(1, round(frame.height * ratio))),
                             Image.LANCZOS)
    buffer = io.BytesIO()
    try:
        frame.save(buffer, format="JPEG", quality=quality, optimize=True,
                   progressive=True)
    except Exception as exc:  # noqa: BLE001 - 编码失败退回原图
        logger.info("表情包压缩跳过（编码失败）：%s", exc)
        return data
    shrunk = buffer.getvalue()
    return shrunk or data


def store_meme_bytes(data: bytes, *, suffix: str) -> tuple[bytes, str]:
    """决定落盘内容与后缀：静态图压缩成 JPEG，GIF 原样保留。"""
    if str(suffix or "").lower() == GIF_SUFFIX:
        return data, suffix
    shrunk = shrink_static_image(data)
    if shrunk is data:  # 压缩失败：保留原图与原后缀，宁可不压也不丢图
        return data, suffix
    return shrunk, ".jpg"
