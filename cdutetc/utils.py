"""通用小工具：消息文本解析与权限判断（纯函数，便于离线测试）。"""

import re

from cdutetc import config
from cdutetc.publish_text import is_publication_question, is_timed_reminder_request

# QQ 群消息中的 @ 片段：OneBot CQ 码 [CQ:at,qq=...] 与旧格式 <@!...>
_AT_PATTERN = re.compile(r"\[CQ:at,qq=[^\]]*\]|<@!?[^>]*>")
_AT_TARGET_PATTERN = re.compile(r"\[CQ:at,qq=([^,\]]+)(?:,[^\]]*)?\]|<@!?([^>]*)>")
# 链接匹配
_LINK_PATTERN = re.compile(r"https?://\S+")


def strip_at(content: str) -> str:
    """去掉消息中的 @ 片段，返回纯文本。"""
    return _AT_PATTERN.sub("", content).strip()


def mentioned_user_ids(content: str) -> set[str]:
    """读取 CQ / 旧格式的 @ 目标，按完整账号标识匹配。"""
    return {
        match.group(1) or match.group(2)
        for match in _AT_TARGET_PATTERN.finditer(content)
    }


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
    return ""
