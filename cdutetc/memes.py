"""QQ 官方富媒体表情包引擎：目录即分类、关键词触发与 file_info 复用。

链路说明（官方协议发图两段式）：
  1) 把公网图片 URL 提交 ``/v2/groups/{group_openid}/files``（file_type=1）换 ``file_info``；
  2) 用 ``msg_type=7`` 的被动回复把 ``file_info`` 发进群。

设计要点：
  - ``assets/memes/<分类>/`` 目录即分类，目录不存在或为空时整条链路静默跳过；
  - ``ACM_MEME_BASE_URL`` 未配置（空串）时不发图，绝不拼出半截地址；
  - ``file_info`` 按 (群, 相对路径) 缓存进 ``meme_file_cache`` 表，24 小时内直接复用，
    避免每次发图都调上传接口；
  - 关键词触发受「25% 概率 + 每群 5 分钟冷却」双重限制，防止刷屏。
"""

from __future__ import annotations

import hashlib
import io
import logging
import random
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from datetime import time as day_time
from pathlib import Path
from urllib.parse import quote, unquote
from zoneinfo import ZoneInfo

from cdutetc import config, database

logger = logging.getLogger("memes")

# 素材根目录：随仓库分发的静态资源，部署时也可用静态服务器单独托管。
MEMES_DIR = Path(__file__).resolve().parent.parent / "assets" / "memes"

# 目录即分类：与 chat_fun.INTERRUPT_CATEGORY 对齐。
CATEGORY_AC = "ac"
CATEGORY_WA_TLE = "wa_tle"
CATEGORY_QUESTION = "question"
CATEGORY_SLACKING = "slacking"
CATEGORY_INTERRUPT = "interrupt"
CATEGORIES = (
    CATEGORY_AC,
    CATEGORY_WA_TLE,
    CATEGORY_QUESTION,
    CATEGORY_SLACKING,
    CATEGORY_INTERRUPT,
)

# 只有这些后缀当作图片；README.md 等占位文件不会被当成素材发出去。
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp")

# 英文高频词必须按词边界匹配：否则 little 会命中 TLE、make 会命中 AK。
_EDGE_LEFT = r"(?<![a-z0-9])"
_EDGE_RIGHT = r"(?![a-z0-9])"

# 命中关键词 → 分类；按顺序取第一条命中的规则。
_KEYWORD_RULES = (
    (CATEGORY_WA_TLE, re.compile(_EDGE_LEFT + "tle" + _EDGE_RIGHT, re.IGNORECASE)),
    (CATEGORY_WA_TLE, re.compile(_EDGE_LEFT + "wa" + _EDGE_RIGHT, re.IGNORECASE)),
    (CATEGORY_WA_TLE, re.compile("爆零|红温")),
    (CATEGORY_AC, re.compile(_EDGE_LEFT + "ak" + _EDGE_RIGHT, re.IGNORECASE)),
    (CATEGORY_AC, re.compile("太强了|膜拜")),
    (CATEGORY_SLACKING, re.compile("摸鱼|下班|早安|晚安")),
    (CATEGORY_QUESTION, re.compile(r"？{2,}|\?{2,}|啥意思")),
)

# 关键词触发的随机概率与每群冷却。
TRIGGER_PROBABILITY = 0.25
GROUP_COOLDOWN_SECONDS = 300.0

# ---------------- 零审核自动收图 ----------------
# 只收「表情包尺度」的图：静态图超过 2MB 基本是拍照原图，收了也是仓库膨胀。
COLLECT_MAX_BYTES = 2 * 1024 * 1024
# 小于 80x80 的多是表情小图标 / 页脚素材，当表情包没效果。
COLLECT_MIN_SIDE = 80
# 宽高比超范围的长条，典型是代码截图、聊天长截图、手机截屏，一律不收。
COLLECT_MAX_ASPECT = 2.2
COLLECT_MIN_ASPECT = 0.45
GIF_SUFFIX = ".gif"

# 落盘前瘦身：QQ 侧拉图慢主要慢在体积，静态图统一缩到 500px 宽存 JPEG。
# MD5 仍然算「下载到的原始字节」，压缩只影响磁盘上的文件，不影响去重口径。
COLLECT_MAX_WIDTH = 500
COLLECT_JPEG_QUALITY = 80

# 收图归类：命中关键词 → (分类, 文件名前缀)。顺序即优先级，先命中先归。
# 英文词按词边界匹配（避免 little 命中 TLE、machine 命中 AC 之类误判）。
_COLLECT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (CATEGORY_WA_TLE, ("WA", "TLE", "RE", "红温", "破防", "大模拟", "寄")),
    (CATEGORY_AC, ("AC", "过了", "绝杀", "tql", "太强", "膜拜")),
    (CATEGORY_QUESTION, ("?", "？", "啥", "迷惑", "离谱")),
)
# 什么关键词都对不上时归入摸鱼：多数群图本来就是闲聊素材。
COLLECT_DEFAULT_CATEGORY = CATEGORY_SLACKING

# 收图上下文窗口：当前消息 + 紧邻的前 1~2 条文本。
COLLECT_CONTEXT_LINES = 2

# ---------------- 场景化主动发图 ----------------
# 场景触发（连续问号 / 饭点 / 深夜）比关键词触发更主动，冷却拉长到 8 分钟。
SCENE_COOLDOWN_SECONDS = 480.0
# 单群最近已发图片的防重队列长度：短期内不重复发同一张。
RECENT_SENT_LIMIT = 10
# 连续问号判定窗口：只看最近几条短句，避免翻旧账。
QUESTION_WINDOW = 5
# 短句上限：长段落里的问号更像提问，不像起哄。
QUESTION_MAX_CHARS = 20

MEAL_WINDOWS = ((day_time(11, 45), day_time(13, 0)), (day_time(17, 30), day_time(19, 0)))
LATE_NIGHT_WINDOWS = ((day_time(23, 30), day_time(2, 0)),)
MEAL_KEYWORDS = ("饿", "吃啥", "吃什么", "外卖", "干饭")
LATE_NIGHT_KEYWORDS = ("困", "睡觉", "睡了", "肝不动")


def match_category(text) -> str:
    """按关键词表判定消息属于哪个情绪分类；无命中返回空串。"""
    if not isinstance(text, str) or not text.strip():
        return ""
    for category, pattern in _KEYWORD_RULES:
        if pattern.search(text):
            return category
    return ""


def _tag_pattern(tag: str) -> re.Pattern | None:
    """纯英文数字关键词编译成词边界正则；中文与标点返回 None 走子串匹配。"""
    clean = str(tag or "")
    if not clean or not re.fullmatch(r"[A-Za-z0-9]+", clean):
        return None
    return re.compile(_EDGE_LEFT + re.escape(clean) + _EDGE_RIGHT, re.IGNORECASE)


def text_hits_tag(text: str, tag: str) -> bool:
    """文本里是否出现该关键词（英文按词边界，中文按子串）。"""
    if not isinstance(text, str) or not text or not tag:
        return False
    pattern = _tag_pattern(tag)
    if pattern is not None:
        return bool(pattern.search(text))
    return str(tag) in text


def sanitize_tag(tag: str) -> str:
    """关键词转成能进文件名的前缀：只留字母数字与汉字，其余丢掉。"""
    clean = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(tag or ""))
    return clean[:16]


def classify_collect(*texts) -> tuple[str, str]:
    """按「当前消息 → 紧邻前几条」的顺序归类，返回（分类, 文件名前缀）。

    都没命中时归入摸鱼，前缀为空串，由调用方用分类名兜底。
    """
    for text in texts:
        if not isinstance(text, str) or not text.strip():
            continue
        for category, tags in _COLLECT_RULES:
            for tag in tags:
                if text_hits_tag(text, tag):
                    return category, sanitize_tag(tag)
    return COLLECT_DEFAULT_CATEGORY, ""


def file_suffix(*candidates) -> str:
    """从文件名 / URL 里推断图片后缀，认不出按 .png 处理。"""
    for candidate in candidates:
        name = str(candidate or "").split("?")[0].split("#")[0]
        if not name:
            continue
        suffix = Path(unquote(name)).suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            return suffix
    return ".png"


def image_md5(data: bytes) -> str:
    """图片内容 MD5：同图不管文件名怎么变都能去重。"""
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


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


def meme_filename(tag: str, md5: str, suffix: str) -> str:
    """收图落盘文件名：``{tag}_{md5[:8]}.{ext}``，前缀也是发图时的匹配依据。"""
    prefix = sanitize_tag(tag)
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

    OneBot 发图需要本机绝对路径，官方协议走 public_url，两条链路共用这一层。
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


_CQ_RE = re.compile(r"\[CQ:[^\]]*\]")


def strip_cq_codes(text: str) -> str:
    """去掉 CQ 码并压缩空白。

    OneBot 的图片消息正文就是 ``[CQ:image,file=xxx]``，若原样进滑窗，
    归类关键词、连问判定与文件名标签都会被这串码污染。
    """
    return " ".join(_CQ_RE.sub(" ", str(text or "")).split())


def extract_image_refs(message) -> list[dict]:
    """从群消息里抠出图片引用：兼容 OneBot 段数组与官方附件两种结构。

    返回 ``[{"url": ..., "file": ...}]``，两者都可能为空，取到哪个用哪个。
    """
    refs: list[dict] = []
    raw = getattr(message, "raw", None)
    if isinstance(raw, dict):
        segments = raw.get("message")
        if isinstance(segments, list):
            for seg in segments:
                if not isinstance(seg, dict) or seg.get("type") != "image":
                    continue
                data = seg.get("data") or {}
                url = str(data.get("url") or "")
                name = str(data.get("file") or "")
                if url or name:
                    refs.append({"url": url, "file": name})
        attachments = raw.get("attachments")
        if isinstance(attachments, list):
            for item in attachments:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "")
                name = str(item.get("filename") or item.get("file") or "")
                if url or name:
                    refs.append({"url": url, "file": name})
    if refs:
        return refs
    # 兜底：raw_message 里的 CQ 码（NapCat 某些配置只给字符串）
    content = getattr(message, "content", "") or ""
    for match in re.finditer(r"\[CQ:image,([^\]]*)\]", content):
        params = dict(
            part.split("=", 1) for part in match.group(1).split(",") if "=" in part
        )
        url = unquote(str(params.get("url") or ""))
        name = unquote(str(params.get("file") or ""))
        if url or name:
            refs.append({"url": url, "file": name})
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


def pick_meme(category: str, *, root: Path | None = None,
              choice: Callable[..., str] = random.choice, exclude: Iterable[str] = (),
              prefer_text: str = "") -> str:
    """取一张素材的相对路径；分类无素材时返回空串。

    选图顺序：先在「未被近期发过」的素材里挑，再看聊天文本是否命中文件名前缀
    （``AC_xxx.png`` 配「终于 AC 了」），都没命中才随机抽。
    """
    names = list_memes(category, root=root)
    if not names:
        return ""
    banned = {str(item) for item in exclude}
    usable = [name for name in names if name not in banned] or names
    if prefer_text:
        matched = [
            name for name in usable
            if filename_tag(name) and text_hits_tag(prefer_text, filename_tag(name))
        ]
        if matched:
            return choice(matched)
    return choice(usable)


def public_url(relative_path: str, *, base_url: str | None = None) -> str:
    """拼出公网可访问的图片地址；BASE_URL 未配置时返回空串，绝不发半截链接。"""
    base = (config.MEME_BASE_URL if base_url is None else base_url) or ""
    base = base.strip().rstrip("/")
    relative = str(relative_path or "").strip().lstrip("/")
    if not base or not relative:
        return ""
    return f"{base}/{quote(relative, safe='/')}"


@dataclass(frozen=True)
class MemeTrigger:
    """一次关键词触发的结论：分类 + 相对路径 + 公网地址。"""

    category: str
    relative_path: str
    url: str


def is_within(window: tuple[day_time, day_time], moment: day_time) -> bool:
    """时刻是否落在时间窗内；支持跨午夜窗（如 23:30-02:00）。"""
    start, end = window
    if start <= end:
        return start <= moment < end
    return moment >= start or moment < end


def slacking_scene(text: str, moment: datetime) -> bool:
    """饭点喊饿、深夜喊困才算摸鱼场景；其他时间不主动打扰。"""
    if not isinstance(text, str) or not text.strip():
        return False
    current = moment.timetz().replace(tzinfo=None)
    if (any(is_within(window, current) for window in MEAL_WINDOWS)
            and any(word in text for word in MEAL_KEYWORDS)):
        return True
    return bool(any(is_within(window, current) for window in LATE_NIGHT_WINDOWS)
                and any(word in text for word in LATE_NIGHT_KEYWORDS))


def is_short_question(text: str) -> bool:
    """短句 + 带问号：起哄式疑问，才值得甩一张问号图。"""
    clean = " ".join(str(text or "").split())
    if not clean or len(clean) > QUESTION_MAX_CHARS:
        return False
    return "?" in clean or "？" in clean


class MemeEngine:
    """发图决策中枢：关键词概率、场景触发、防重队列与素材目录均可注入。"""

    def __init__(
        self,
        *,
        probability: float = TRIGGER_PROBABILITY,
        cooldown_seconds: float = GROUP_COOLDOWN_SECONDS,
        scene_cooldown_seconds: float = SCENE_COOLDOWN_SECONDS,
        recent_limit: int = RECENT_SENT_LIMIT,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
        root: Path | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.probability = probability
        self.cooldown_seconds = cooldown_seconds
        self.scene_cooldown_seconds = scene_cooldown_seconds
        self.recent_limit = recent_limit
        self._clock = clock
        self._rng = rng
        self._root = root
        self._now = now_provider or (lambda: datetime.now(ZoneInfo(config.TZ_NAME)))
        self._last_at: dict[str, float] = {}
        self._scene_at: dict[str, float] = {}
        self._recent: dict[str, deque[str]] = {}
        self._window: dict[str, deque[str]] = {}

    # ---------------- 关键词触发 ----------------
    def should_trigger(self, group_id: str, text: str, *,
                       now: float | None = None) -> str:
        """命中关键词且通过冷却与概率时返回分类名，否则返回空串。

        冷却先于概率判断：冷却期内的消息既不掷骰子也不刷新冷却时间。
        """
        category = match_category(text)
        if not category:
            return ""
        group = str(group_id or "").strip()
        if not group:
            return ""
        moment = self._clock() if now is None else now
        last = self._last_at.get(group)
        if last is not None and moment - last < self.cooldown_seconds:
            return ""
        if self._rng() >= self.probability:
            return ""
        self._last_at[group] = moment
        return category

    # ---------------- 场景触发 ----------------
    def remember_text(self, group_id: str, text: str) -> None:
        """把群聊文本塞进内存滑窗：只留最近几条短句，供连续问号判定。"""
        group = str(group_id or "").strip()
        clean = " ".join(str(text or "").split())
        if not group or not clean:
            return
        window = self._window.setdefault(group, deque(maxlen=QUESTION_WINDOW))
        window.append(clean)

    def recent_texts(self, group_id: str) -> list[str]:
        """当前群的内存滑窗文本（旧 → 新）。"""
        return list(self._window.get(str(group_id or "").strip(), ()))

    def scene_trigger(self, group_id: str, text: str, *, now: float | None = None,
                      moment: datetime | None = None,
                      remember: bool = True) -> str:
        """场景触发：连续问号 → question，饭点/深夜关键词 → slacking。

        默认会先记一次文本滑窗（调用方不要再重复 remember_text）；
        调用方已统一维护滑窗时传 ``remember=False``，避免同一条消息进窗两次。
        """
        group = str(group_id or "").strip()
        if not group:
            return ""
        if remember:
            self.remember_text(group, text)
        category = ""
        recent = self.recent_texts(group)[-2:]
        if len(recent) == 2 and all(is_short_question(item) for item in recent):
            category = CATEGORY_QUESTION
        else:
            current = self._now() if moment is None else moment
            if slacking_scene(text, current):
                category = CATEGORY_SLACKING
        if not category:
            return ""
        stamp = self._clock() if now is None else now
        last = self._scene_at.get(group)
        if last is not None and stamp - last < self.scene_cooldown_seconds:
            return ""
        self._scene_at[group] = stamp
        return category

    # ---------------- 选图与防重 ----------------
    def recent_sent(self, group_id: str) -> list[str]:
        """该群最近发过的素材相对路径（旧 → 新）。"""
        return list(self._recent.get(str(group_id or "").strip(), ()))

    def record_sent(self, group_id: str, relative_path: str) -> None:
        """登记一张刚发出的图，超出队列长度自动淘汰最旧的。"""
        group = str(group_id or "").strip()
        relative = str(relative_path or "").strip()
        if not group or not relative:
            return
        sent = self._recent.setdefault(group, deque(maxlen=self.recent_limit))
        sent.append(relative)

    def recall_last_sent(self, group_id: str) -> str:
        """取该群最近一次发出的素材路径（/删图 的目标），不发不返回。"""
        sent = self._recent.get(str(group_id or "").strip())
        return sent[-1] if sent else ""

    def forget_sent(self, group_id: str, relative_path: str) -> None:
        """把一张已被删除的图从防重队列里摘掉。"""
        sent = self._recent.get(str(group_id or "").strip())
        if not sent:
            return
        remaining = [item for item in sent if item != str(relative_path or "").strip()]
        self._recent[str(group_id).strip()] = deque(remaining, maxlen=self.recent_limit)

    def resolve(self, category: str, *, group_id: str = "",
                text: str = "", require_url: bool = True) -> MemeTrigger | None:
        """把分类解析成可发送的（相对路径, 公网地址）；任一步缺失即返回 None。

        ``group_id`` 用于避开该群最近发过的图，``text`` 用于优先命中标签前缀。
        ``require_url=False`` 供 OneBot 使用：直接发本机文件，不需要公网地址。
        """
        group = str(group_id or "").strip()
        exclude = self.recent_sent(group) if group else ()
        relative = pick_meme(category, root=self._root, exclude=exclude,
                             prefer_text=text)
        if not relative:
            return None
        url = public_url(relative)
        if not url and require_url:
            return None
        return MemeTrigger(category=category, relative_path=relative, url=url)

    def reset(self, group_id: str | None = None) -> None:
        if group_id is None:
            self._last_at.clear()
            self._scene_at.clear()
            self._recent.clear()
            self._window.clear()
            return
        group = str(group_id)
        self._last_at.pop(group, None)
        self._scene_at.pop(group, None)
        self._recent.pop(group, None)
        self._window.pop(group, None)


_default_engine: MemeEngine | None = None


def get_engine() -> MemeEngine:
    """进程级单例：每群冷却状态必须跨消息保留。"""
    global _default_engine
    if _default_engine is None:
        _default_engine = MemeEngine()
    return _default_engine


def reset_engine() -> None:
    """丢弃单例（测试隔离用）。"""
    global _default_engine
    _default_engine = None


# =====================================================================
# 零审核自动收图：下载 → 去重 → 过滤 → 归类 → 落盘 → 记账
# =====================================================================


async def collect_one_image(ref: dict, *, group_id: str, text: str = "",
                            context: Iterable[str] = (), root: Path | None = None,
                            downloader: Callable[[str], Awaitable[bytes]] | None = None,
                            ) -> dict | None:
    """处理单张图：任何一步不合格就返回 None，绝不让异常冒到主流程。

    返回 ``{"md5", "category", "tag", "relative_path"}`` 供日志/回执使用。
    """
    url = str((ref or {}).get("url") or "")
    name = str((ref or {}).get("file") or "")
    fetch = downloader or fetch_image_bytes
    data = b""
    if url:
        try:
            data = await fetch(url)
        except Exception as exc:  # noqa: BLE001 - 下载失败只跳过这一张
            logger.info("收图下载异常：%s", exc)
            return None
    if not data:
        return None
    md5 = image_md5(data)
    try:
        if database.meme_hash_exists(md5):
            # 含 deleted=1 的黑名单：删过的图不再回收
            return None
    except Exception as exc:  # noqa: BLE001 - 台账读不到时宁可不收
        logger.info("收图去重查询失败：%s", exc)
        return None
    suffix = file_suffix(name, url)
    accepted, reason = should_collect(data, suffix=suffix)
    if not accepted:
        logger.info("收图跳过（%s）：%s", reason, (name or url)[:80])
        return None
    lines = [text, *[str(item) for item in context][:COLLECT_CONTEXT_LINES]]
    category, tag = classify_collect(*lines)
    # 落盘前瘦身：静态图压成 500px 宽 JPEG，GIF 原样；MD5 仍用原始字节算。
    payload, store_suffix = store_meme_bytes(data, suffix=suffix)
    relative = save_meme_file(payload, category=category, md5=md5, tag=tag,
                              suffix=store_suffix, root=root)
    if not relative:
        return None
    try:
        database.save_meme_hash(md5, category, tag or category, relative)
    except Exception as exc:  # noqa: BLE001 - 记账失败就当没收，避免重复落盘
        logger.warning("收图记账失败：%s", exc)
        delete_meme_file(relative, root=root)
        return None
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
    refs = extract_image_refs(message)
    if not refs:
        return []
    # 文本与上下文统一去 CQ 码：图片消息的正文就是 CQ 串，不能拿它当聊天内容。
    clean_text = strip_cq_codes(text)
    clean_context = [item for item in map(strip_cq_codes, context) if item]
    collected: list[dict] = []
    for ref in refs:
        outcome = await collect_one_image(
            ref, group_id=group, text=clean_text, context=clean_context,
            root=root, downloader=downloader,
        )
        if outcome:
            collected.append(outcome)
    return collected


def delete_last_sent(group_id: str, *, engine: MemeEngine | None = None,
                     root: Path | None = None) -> dict | None:
    """删掉该群最近一次发出的表情包文件，并把 MD5 拉黑。

    机器人发的可能是原始素材（没进台账），此时只删文件、返回空 MD5。
    没有任何发送记录时返回 None，由指令层回执「没图可删」。
    """
    target_engine = engine or get_engine()
    relative = target_engine.recall_last_sent(group_id)
    if not relative:
        return None
    delete_meme_file(relative, root=root)
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

