"""通用小工具：消息文本解析与权限判断（纯函数，便于离线测试）。"""

import re

from cdutetc import config
from cdutetc.publish_text import is_publication_question, is_timed_reminder_request

# QQ 群消息中的 @ 片段：OneBot CQ 码 [CQ:at,qq=...] 与官方协议 <@id>。
# 官方协议的 id 是 openid（非纯数字），因此不能按数字匹配；
# 旧版 botpy 的 <@!id> 文本语法不会再产生，故不再兼容解析（(?!\!) 排除）。
_AT_PATTERN = re.compile(r"\[CQ:at,qq=[^\]]*\]|<@(?!\!)[^>\s]+>")
_AT_TARGET_PATTERN = re.compile(r"\[CQ:at,qq=([^,\]]+)(?:,[^\]]*)?\]|<@(?!\!)([^>\s]+)>")
# 链接匹配
_LINK_PATTERN = re.compile(r"https?://\S+")


def strip_at(content: str) -> str:
    """去掉消息中的 @ 片段，返回纯文本。"""
    return _AT_PATTERN.sub("", content).strip()


def mentioned_user_ids(content: str) -> set[str]:
    """读取 @ 目标（CQ 码或 <@id>），按完整账号标识匹配。"""
    return {m.group(1) or m.group(2) for m in _AT_TARGET_PATTERN.finditer(content)}


def message_mentions(message) -> list[dict]:
    """读取消息的统一 @ 载体。

    客户端适配层已把平台 mention 规范成 ``[{"id", "is_you"}]``
    （契约见 ``clients/base.py``）；缺失该字段的旧对象回退解析文本 @ 片段。
    """
    mentions = getattr(message, "mentions", None)
    # 适配层未给出（或给不出）@ 目标时，回退解析正文，避免客户端解析口径不全导致漏判。
    if not mentions:
        content = getattr(message, "content", "") or ""
        return [{"id": target, "is_you": False} for target in mentioned_user_ids(content)]
    normalized: list[dict] = []
    for item in mentions:
        if isinstance(item, dict):
            ident, is_you = item.get("id"), item.get("is_you")
        else:
            ident, is_you = getattr(item, "id", ""), getattr(item, "is_you", False)
        normalized.append({"id": str(ident or ""), "is_you": is_you is True})
    return normalized


def is_addressed_to_bot(message, bot_identity: str = "") -> bool:
    """判定本条消息是否 @ 到本机器人（协议无关）。

    官方协议的 mention 带平台 is_you 标记，直接采信；OneBot 的 @ 目标是
    QQ 号，用自身账号比对。两条路径统一走本函数，业务层不再自解析。
    """
    identity = str(bot_identity or "")
    mentions = message_mentions(message)
    if any(item["is_you"] for item in mentions):
        return True
    if identity and any(item["id"] == identity for item in mentions):
        return True
    return bool(getattr(message, "always_addressed", False))


def extract_url(content: str) -> str | None:
    """从文本中提取第一个 http(s) 链接，没有则返回 None。"""
    m = _LINK_PATTERN.search(content)
    return m.group(0) if m else None


def contains_link(content: str) -> bool:
    """判断文本是否包含链接。"""
    return bool(_LINK_PATTERN.search(content))


# 发布命令的可用写法（老师习惯用斜杠，也习惯叫“添加比赛”）。
# 顺序按长度从大到小，避免短前缀先命中长命令。
_NATURAL_PUBLISH_PREFIXES = ("/添加一个比赛", "添加一个比赛", "/添加一场比赛", "添加一场比赛", "/添加个比赛", "添加个比赛")
_PUBLISH_PREFIXES = (*_NATURAL_PUBLISH_PREFIXES, "/添加比赛", "添加比赛", "/发布", "发布")

# 命令词后面允许出现的分隔符：空白与各种标点。
# 中文输入法下「/添加比赛，15分钟…」很常见，不能因为它没空格就拒绝。
_CMD_SEPARATORS = " \t\r\n\u3000,，.。:：、;；!！?？~～"


def match_publish_command(text: str, cmd: str = "发布") -> str | None:
    """识别发布命令并返回正文；不是发布命令返回 None。

    支持 `发布 …`、`/发布 …`、`添加比赛 …`、`/添加比赛 …` 四种写法，
    命令词后允许空白或中英文标点做分隔（兼容中文输入法习惯）。
    命令词后紧跟其它文字时不认，避免把「发布什么比赛」这类提问当命令。
    """
    s = (text or "").strip()
    prefixes = list(_PUBLISH_PREFIXES)
    if cmd and f"/{cmd}" not in prefixes:
        prefixes = [f"/{cmd}", cmd, *prefixes]
    for prefix in prefixes:
        if not s.startswith(prefix):
            continue
        rest = s[len(prefix):]
        if not rest:
            return ""
        if prefix in _NATURAL_PUBLISH_PREFIXES:
            return None if is_publication_question(rest) else rest.strip(_CMD_SEPARATORS).strip()
        if (rest[0] in _CMD_SEPARATORS
                or rest.startswith(("http://", "https://", "www."))):
            return rest.strip(_CMD_SEPARATORS).strip()
        continue
    return s if is_timed_reminder_request(s) else None


def is_valid_account_id(value: str | None) -> bool:
    """群/人标识是否合法。

    兼容两种接入协议：
    - OneBot（NapCat）：QQ 群号 / QQ 号，纯数字；
    - QQ 官方机器人：group_openid / member_openid，十六进制字母数字混排。
    因此不能只判断 isdigit，否则官方机器人的 openid 会被误判为非法。
    """
    if not value:
        return False
    text = str(value).strip()
    if not text or len(text) > 64:
        return False
    return all(ch.isalnum() or ch in "_-" for ch in text)


def is_allowed_group(group_id: str | None, allowed_groups: set[str]) -> bool:
    """群是否在服务白名单。未配置白名单 = 不允许任何群。"""
    return bool(group_id) and group_id in allowed_groups


def parse_command(text: str) -> tuple[str | None, str]:
    """解析命令文本为 (命令名, 剩余参数)。

    - 必须以 / 开头，如 “/订阅 蓝桥杯” → ("订阅", "蓝桥杯")。
    - “/帮助”“/help” → ("帮助", "")。
    - 命令名保留原始大小写，避免中文命令（如 /FAQ添加）被 lower 破坏；
      英文命令的大小写兼容由命令分发表做不敏感兜底（见 commands.handle_command）。
    - 非命令文本返回 (None, "")。
    """
    s = text.strip()
    if not s.startswith("/"):
        return None, ""
    rest = s[1:].strip()
    if not rest:
        return None, ""
    parts = rest.split(None, 1)
    cmd = parts[0]
    args = parts[1].strip() if len(parts) > 1 else ""
    if cmd.lower() in ("help", "帮助"):
        return "帮助", args
    return cmd, args


def is_super_admin(member_id: str | None) -> bool:
    """超级管理员 = ACM_SUPER_ADMIN 白名单成员（/修正 纠正比赛、管理全局信息）。
    默认全拒绝；身份一律来自 SDK 事件字段，禁止相信消息正文。"""
    return bool(member_id) and member_id in config.SUPER_ADMIN


def is_teacher(member_id: str | None) -> bool:
    """老师 = 教师白名单成员。默认全拒绝。"""
    return bool(member_id) and member_id in config.TEACHER_WHITELIST


def can_manage_group(member_id: str | None) -> bool:
    """老师和超管可管理课程/FAQ及查看管理状态；普通成员默认拒绝。"""
    return is_teacher(member_id) or is_super_admin(member_id)


def admin_group_id() -> str:
    """私聊管理命令作用的默认群：优先 ACM_ADMIN_GROUP，留空取允许群首个。"""
    if config.ADMIN_GROUP:
        return config.ADMIN_GROUP
    if config.ALLOWED_GROUPS:
        return sorted(config.ALLOWED_GROUPS)[0]


# ---------------------------------------------------------------------
# 时间表达归一化：口语/半结构化输入 → 统一的 周X + HH:MM[-HH:MM]
# ---------------------------------------------------------------------

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
              "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_WEEKDAY_PREFIXES = (
    re.compile(r"^\s*每?\s*(?:星期|礼拜|周)\s*([一二三四五六日天])\s*"),
    re.compile(r"^\s*每\s*([一二三四五六日天])\s*"),
)
_CN_HOUR = re.compile(r"([零〇一二两三四五六七八九十]{1,3})(?=\s*(?:点|時|时|[:：]|分|半))")
_SPAN_SPLIT = re.compile(r"\s*(?:-|－|—|–|~|～|到|至)\s*")
_POINT = re.compile(
    r"^(?:(?P<period>凌晨|早上|上午|中午|下午|傍晚|晚上|夜间)\s*)?"
    r"(?P<hour>\d{1,2})"
    r"(?:\s*(?:[:：点时時])\s*(?:(?P<minute>\d{1,2})\s*分?|(?P<half>半))?)?$"
)
_NORMALIZED_TIME = re.compile(r"^\d{2}:\d{2}$")
_PM_PERIODS = {"下午", "傍晚", "晚上", "夜间"}
_NOON_PERIODS = {"中午"}


def _cn_to_int(text: str) -> int:
    """中文数字（1~99）转整数：十 / 十二 / 二十 / 两。"""
    if "十" in text:
        tens, _, ones = text.partition("十")
        base = (_CN_DIGITS.get(tens, 1) if tens else 1) * 10
        return base + (_CN_DIGITS.get(ones, 0) if ones else 0)
    value = 0
    for char in text:
        value = value * 10 + _CN_DIGITS.get(char, 0)
    return value


def _split_weekday(text: str) -> tuple[str, str]:
    """剥离开头的星期表达（周四/星期四/礼拜四/每周四/每四），回归为 周X。"""
    for pattern in _WEEKDAY_PREFIXES:
        match = pattern.match(text)
        if match:
            day = "日" if match.group(1) == "天" else match.group(1)
            return f"周{day}", text[match.end():]
    return "", text


def _parse_point(text: str, inherited_period: str = "") -> tuple[int, int, str] | None:
    """解析单个时间点，返回 (时, 分, 生效的时段前缀)；无法解析返回 None。"""
    match = _POINT.match(text.strip())
    if not match:
        return None
    period = match.group("period") or inherited_period
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or (30 if match.group("half") else 0))
    if period in _PM_PERIODS and hour < 12:
        hour += 12
    elif period in _NOON_PERIODS and hour <= 10:
        hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute, period


def _normalize_span(text: str) -> str | None:
    """归一化「起点[-终点]」，失败返回 None（调用方保留原文）。"""
    chunks = [chunk for chunk in _SPAN_SPLIT.split(text.strip()) if chunk.strip()]
    if not chunks or len(chunks) > 2:
        return None
    points: list[str] = []
    inherited = ""
    for chunk in chunks:
        parsed = _parse_point(chunk, inherited)
        if parsed is None:
            return None
        hour, minute, inherited = parsed
        points.append(f"{hour:02d}:{minute:02d}")
    return "-".join(points)


def normalize_time_expression(raw: str) -> str:
    """把「周四 19-21点」这类表达归一为「周四 19:00-21:00」。

    支持：中文数字（两点半/十九点）、时段前缀（下午/晚上/中午/凌晨，可跨段继承）、
    半/刻钟写法（点半）、区间分隔符（- ~ ～ 到 至）。无法识别时原样返回，绝不猜测。
    """
    original = (raw or "").strip()
    if not original:
        return original
    weekday, rest = _split_weekday(original)
    rest = _CN_HOUR.sub(lambda match: str(_cn_to_int(match.group(1))), rest)
    span = _normalize_span(rest)
    if span is None:
        return original
    return f"{weekday} {span}" if weekday else span


def split_normalized_time(text: str) -> tuple[str, str, str]:
    """归一化后拆成 (周几, 开始, 结束)；无法识别时三项均为空串。

    用于课程写入：库里统一存 周X + HH:MM/HH:MM，展示侧再加中文格式。
    """
    normalized = normalize_time_expression(text)
    weekday, rest = _split_weekday(normalized)
    parts = [part.strip() for part in rest.split("-") if part.strip()]
    if not parts or len(parts) > 2 or not all(_NORMALIZED_TIME.match(part) for part in parts):
        return "", "", ""
    return weekday, parts[0], parts[1] if len(parts) == 2 else ""
    return ""
