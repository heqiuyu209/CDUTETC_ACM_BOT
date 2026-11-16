"""表情包规则层：分类目录、关键词与归类规则，以及全部容量/概率阈值。

本模块是素材体系的单一事实源（常量集中在此），其余子模块按需静态导入。
"""

from __future__ import annotations

import re
from datetime import time as day_time
from pathlib import Path
from urllib.parse import unquote

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

# ---------------- OCR 文字体检（拦截截图） ----------------
# 图内「有效字符」超过这个数，基本是聊天截屏 / 网页截图而不是表情包。
OCR_MAX_CHARS = 35
# 代码、题面截图的特征串：命中任一即视为「不是表情包」。
OCR_CODE_HINTS = ("#include", "int main", "std::", "public static",
                  "Traceback", "Time Limit", "Memory Limit")
# 符号密度兜底：代码/题面即使不命中特征串，也躲不过这堆符号的堆叠。
_OCR_CODE_SYMBOLS = (";", "{", "}")
OCR_CODE_SYMBOL_MIN = 3
# OCR 置信度下限：低于它的碎片多是背景噪声，不该影响归类与拦截。
_OCR_MIN_SCORE = 0.5

# ---------------- 相似图去重（dHash） ----------------
# 感知哈希按 9x8 灰度比较相邻像素，得到 64 位（16 位十六进制）。
DHASH_SIZE = 8
# 汉明距离阈值：<=4 视为同一张图的不同压缩/缩放版本。
DHASH_MAX_DISTANCE = 4

# ---------------- 单人收图限流 ----------------
# 同一 (群, 用户) 在窗口内最多成功收录几张，超出静默跳过，避免单人刷屏灌图。
COLLECT_USER_WINDOW_SECONDS = 60.0
COLLECT_USER_MAX_IMAGES = 2

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
