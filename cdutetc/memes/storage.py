"""存储层：素材目录与命名、内容哈希/感知哈希去重、分类淘汰与单人限流台账。"""

from __future__ import annotations

import hashlib
import io
import logging
import time
from collections import deque
from pathlib import Path

import cdutetc.memes as _facade
from cdutetc import config, database
from cdutetc.memes.rules import (
    CATEGORIES,
    COLLECT_USER_MAX_IMAGES,
    COLLECT_USER_WINDOW_SECONDS,
    DHASH_MAX_DISTANCE,
    DHASH_SIZE,
    IMAGE_SUFFIXES,
)

logger = logging.getLogger("memes")

MEMES_DIR = Path(__file__).resolve().parent.parent.parent / "assets" / "memes"
_recent_collects: dict[tuple[str, str], deque] = {}
def image_md5(data: bytes) -> str:
    """图片内容 MD5：同图不管文件名怎么变都能去重。"""
    return hashlib.md5(data, usedforsecurity=False).hexdigest()
def compute_dhash(data: bytes) -> str:
    """算 64 位感知哈希（dHash），返回 16 位十六进制小写串；算不出返回空串。

    做法：灰度化 + 缩到 9x8，逐行比较相邻像素大小，得到 8x8=64 个比特。
    这样「同一张图被重新压缩 / 缩放」也能落到很近的距离上。
    """
    if not data:
        return ""
    try:
        from PIL import Image
    except ImportError:  # 部署环境没装 Pillow：不做相似图去重，只按 MD5 查重
        return ""
    try:
        with Image.open(io.BytesIO(data)) as image:
            # GIF 只取首帧：动图表情包的主体通常就在第一帧。
            gray = image.convert("L").resize((DHASH_SIZE + 1, DHASH_SIZE), Image.LANCZOS)
            pixels = list(gray.getdata())
    except Exception:  # noqa: BLE001 - 损坏图/伪装图无哈希
        return ""
    if len(pixels) < (DHASH_SIZE + 1) * DHASH_SIZE:
        return ""
    bits = 0
    for row in range(DHASH_SIZE):
        base = row * (DHASH_SIZE + 1)
        for col in range(DHASH_SIZE):
            bits = (bits << 1) | (1 if pixels[base + col] > pixels[base + col + 1] else 0)
    return f"{bits:016x}"


def dhash_distance(left: str, right: str) -> int:
    """两个 dHash 的汉明距离；任一为空或格式不合时返回「最远」以判不等。"""
    left_text = str(left or "").strip().lower()
    right_text = str(right or "").strip().lower()
    if not left_text or not right_text or len(left_text) != len(right_text):
        return DHASH_SIZE * DHASH_SIZE
    try:
        return bin(int(left_text, 16) ^ int(right_text, 16)).count("1")
    except ValueError:
        return DHASH_SIZE * DHASH_SIZE


def find_similar_meme(dhash: str, *, include_deleted: bool = True) -> str:
    """在收图台账里找与 ``dhash`` 足够像的记录，返回命中记录的 MD5（无命中空串）。

    默认连 deleted=1 的黑名单一起比：删掉的图被重新压缩再发一次，也不该收回来。
    """
    target = str(dhash or "").strip()
    if not target:
        return ""
    try:
        records = database.list_meme_dhashes(include_deleted=include_deleted)
    except Exception as exc:
        logger.info("相似图去重查询失败：%s", exc)
        raise
    for record in records:
        if dhash_distance(target, record.get("dhash") or "") <= DHASH_MAX_DISTANCE:
            return str(record.get("md5") or "")
    return ""


def _category_cap() -> int:
    """当前生效的单分类上限：读 config 动态值，便于测试与热调整。"""
    raw = getattr(config, "MEME_CATEGORY_CAP", 100)
    try:
        cap = int(raw)
    except (TypeError, ValueError):
        return 100
    return max(0, cap)


def enforce_category_cap(category: str, *, root: Path | None = None,
                         cap: int | None = None) -> list[str]:
    """把分类内素材压到上限内：从最旧的开始删文件并标记 cleaned，返回被淘汰的路径。

    淘汰只腾容量、不进黑名单（cleaned=1 而非 deleted=1），同图再次出现仍可收录。
    任何异常都不上抛——收图主流程不能因为整理失败而丢图。
    """
    limit = _category_cap() if cap is None else int(cap)
    if limit <= 0 or not category:
        return []
    removed: list[str] = []
    try:
        while database.count_meme_hashes(category=category) > limit:
            oldest = database.oldest_meme_hashes(category, limit=1)
            if not oldest:
                break
            record = oldest[0]
            relative = str(record.get("file_path") or "")
            if relative:
                delete_meme_file(relative, root=root)
            database.mark_meme_hash_cleaned(record.get("md5") or "")
            removed.append(relative)
            logger.info("分类 %s 超限 %d，淘汰最旧素材：%s", category, limit, relative)
    except Exception as exc:  # noqa: BLE001 - 淘汰失败不影响本次收图
        logger.warning("分类 %s 超限淘汰失败：%s", category, exc)
    return removed


def _collect_quota_left(group_id: str, user_id: str, *, now: float | None = None) -> int:
    """该 (群, 用户) 在当前窗口内还能收几张；无用户身份时不做限流（返回极大值）。"""
    user = str(user_id or "").strip()
    if not user:
        return COLLECT_USER_MAX_IMAGES
    moment = time.monotonic() if now is None else now
    bucket = _recent_collects.get((str(group_id or ""), user))
    if not bucket:
        return COLLECT_USER_MAX_IMAGES
    cutoff = moment - COLLECT_USER_WINDOW_SECONDS
    while bucket and bucket[0] <= cutoff:
        bucket.popleft()
    return max(0, COLLECT_USER_MAX_IMAGES - len(bucket))


def _record_collect(group_id: str, user_id: str, *, now: float | None = None) -> None:
    """记一次「成功收录」，只统计真正落盘的图，被拦下的不占额度。"""
    user = str(user_id or "").strip()
    if not user:
        return
    moment = time.monotonic() if now is None else now
    key = (str(group_id or ""), user)
    bucket = _recent_collects.setdefault(key, deque())
    cutoff = moment - COLLECT_USER_WINDOW_SECONDS
    while bucket and bucket[0] <= cutoff:
        bucket.popleft()
    bucket.append(moment)


def reset_collect_limits() -> None:
    """清空单人限流台账（重启/测试用）。"""
    _recent_collects.clear()
def meme_filename(tag: str, md5: str, suffix: str) -> str:
    """收图落盘文件名：``{tag}_{md5[:8]}.{ext}``，前缀也是发图时的匹配依据。"""
    prefix = _facade.sanitize_tag(tag)
    digest = str(md5 or "")[:8]
    ext = str(suffix or ".png").lower()
    if not ext.startswith("."):
        ext = "." + ext
    return f"{prefix}_{digest}{ext}" if prefix else f"{digest}{ext}"


def save_meme_file(data: bytes, *, category: str, md5: str, tag: str,
                   suffix: str, root: Path | None = None) -> str:
    """把收来的图写进素材目录，返回相对素材根的 POSIX 路径；失败返回空串。"""
    if category not in CATEGORIES:
        return ""
    base = MEMES_DIR if root is None else Path(root)
    directory = base / category
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / meme_filename(tag, md5, suffix)).write_bytes(data)
    except OSError as exc:
        logger.warning("收图落盘失败：%s", exc)
        return ""
    return f"{category}/{meme_filename(tag, md5, suffix)}"


def meme_path(relative_path: str, *, root: Path | None = None) -> Path | None:
    """把素材相对路径解析成绝对路径；越界或为空时返回 None。

    本地文件用于收图和删除校验，发图通过 public_url 上传至官方接口。
    """
    relative = str(relative_path or "").strip().lstrip("/")
    if not relative or ".." in relative:
        return None
    base = MEMES_DIR if root is None else Path(root)
    target = (base / relative).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError:
        return None
    return target if target.is_file() else None


def delete_meme_file(relative_path: str, *, root: Path | None = None) -> bool:
    """删除一张已收素材（/删图 用），文件不存在也按删除成功处理。"""
    relative = str(relative_path or "").strip().lstrip("/")
    if not relative or ".." in relative:
        return False
    base = MEMES_DIR if root is None else Path(root)
    target = base / relative
    try:
        target.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("删图失败：%s", exc)
        return False
    return True
def list_memes(category: str, *, root: Path | None = None) -> list[str]:
    """列出某分类下的可用素材，返回相对素材根目录的 POSIX 风格路径。

    目录不存在、不是目录或没有图片时统一返回空列表（调用方据此静默跳过）。
    """
    if category not in CATEGORIES:
        return []
    base = MEMES_DIR if root is None else Path(root)
    directory = base / category
    if not directory.is_dir():
        return []
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    names = [
        f"{category}/{path.name}"
        for path in entries
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    ]
    return sorted(names)


def filename_tag(relative_path: str) -> str:
    """取文件名里的标签前缀：``wa_tle/WA_1a2b3c4d.png`` → ``WA``。"""
    stem = Path(str(relative_path or "")).stem
    if "_" not in stem:
        return ""
    return stem.split("_", 1)[0]
