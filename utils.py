"""通用小工具：消息文本解析与权限判断（纯函数，便于离线测试）。"""

import re

import config

# QQ 群消息中的 @ 片段：OneBot CQ 码 [CQ:at,qq=...] 与旧格式 <@!...>
_AT_PATTERN = re.compile(r"\[CQ:at,qq=[^\]]*\]|<@!?[^>]*>")
# 链接匹配
_LINK_PATTERN = re.compile(r"https?://\S+")


def strip_at(content: str) -> str:
    """去掉消息中的 @ 片段，返回纯文本。"""
    return _AT_PATTERN.sub("", content).strip()


def extract_url(content: str) -> str | None:
    """从文本中提取第一个 http(s) 链接，没有则返回 None。"""
    m = _LINK_PATTERN.search(content)
    return m.group(0) if m else None


def contains_link(content: str) -> bool:
    """判断文本是否包含链接。"""
    return bool(_LINK_PATTERN.search(content))


def is_publish_command(text: str, cmd: str = "发布") -> bool:
    """完整命令词后须为空、空白或 HTTP 链接，避免误认普通提问。"""
    if not cmd or not text.startswith(cmd):
        return False
    rest = text[len(cmd):]
    return not rest or rest[0].isspace() or rest.startswith(("http://", "https://"))


def is_allowed_group(group_id: str | None, allowed_groups: set[str]) -> bool:
    """群是否在服务白名单。未配置白名单 = 不允许任何群。"""
    return bool(group_id) and group_id in allowed_groups


def can_publish(member_id: str | None, whitelist: set[str]) -> bool:
    """成员是否有发布权限。未配置白名单 = 不允许任何人发布。

    身份一律来自 SDK 事件对象（member_openid），禁止相信消息正文中的身份声明。
    """
    return bool(member_id) and member_id in whitelist


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


def is_admin(member_id: str | None) -> bool:
    """兼容别名：管理员即超级管理员。默认全拒绝。"""
    return is_super_admin(member_id)


def is_super_admin(member_id: str | None) -> bool:
    """超级管理员 = ACM_SUPER_ADMIN 白名单成员（/修正 纠正比赛、管理全局信息）。
    默认全拒绝；身份一律来自 SDK 事件字段，禁止相信消息正文。"""
    return bool(member_id) and member_id in config.SUPER_ADMIN


def is_teacher(member_id: str | None) -> bool:
    """老师 = 教师白名单成员（可免审发布比赛/管理课程/FAQ/发起征集/查看征集汇总）。
    默认全拒绝。"""
    return bool(member_id) and member_id in config.TEACHER_WHITELIST
