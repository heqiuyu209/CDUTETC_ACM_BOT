"""运行时角色解析：把 .env 基线与数据库里的动态白名单合并成生效集合。

设计要点：
- `config.ENV_ALLOWED_GROUPS` / `ENV_TEACHER_WHITELIST` 是配置文件基线，永不修改；
- 数据库 `runtime_allowed_groups` / `runtime_teachers` 存超管用命令动态添加的条目；
- 生效值写回 `config.ALLOWED_GROUPS` / `config.TEACHER_WHITELIST`，
  这样所有既有代码（`is_allowed_group`、`can_manage_group` 等）无需改动；
- 超管白名单不参与动态合并，只能改 `ACM_SUPER_ADMIN` 并重启，
  防止“提权命令给自己发权限”。

任何一次增删都要调用一次 `refresh()`，改动立即生效、无需重启。
"""

import logging

import config
import database as db

logger = logging.getLogger(__name__)


def refresh() -> None:
    """按「环境变量基线 ∪ 数据库动态白名单」重算生效集合。"""
    groups = set(config.ENV_ALLOWED_GROUPS)
    groups.update(item["group_id"] for item in db.list_runtime_groups())
    teachers = set(config.ENV_TEACHER_WHITELIST)
    teachers.update(item["user_id"] for item in db.list_runtime_teachers())
    config.ALLOWED_GROUPS = groups
    config.TEACHER_WHITELIST = teachers
    logger.info(
        "白名单已刷新：群 %d 个（基线 %d + 动态 %d），老师 %d 人（基线 %d + 动态 %d）",
        len(groups), len(config.ENV_ALLOWED_GROUPS),
        len(db.list_runtime_groups()),
        len(teachers), len(config.ENV_TEACHER_WHITELIST),
        len(db.list_runtime_teachers()),
    )


def add_group(group_id: str, operator: str = "", note: str = "") -> tuple[bool, str]:
    """添加允许群。Returns: (是否新增, 说明)"""
    gid = (group_id or "").strip()
    if not gid:
        return False, "群号不能为空"
    if gid in config.ENV_ALLOWED_GROUPS:
        return False, f"群 {gid} 已在配置文件中，无需重复添加"
    if not gid.isdigit():
        return False, "群号应为纯数字"
    if not db.add_runtime_group(gid, operator=operator, note=note):
        return False, f"群 {gid} 已在允许列表中"
    refresh()
    return True, f"已添加群 {gid}，立即生效"


def remove_group(group_id: str, operator: str = "") -> tuple[bool, str]:
    """删除动态添加的允许群。"""
    gid = (group_id or "").strip()
    if gid in config.ENV_ALLOWED_GROUPS:
        return False, f"群 {gid} 来自配置文件，请修改 ACM_ALLOWED_GROUPS 并重启"
    if not db.remove_runtime_group(gid, operator=operator):
        return False, f"群 {gid} 不在动态白名单中"
    refresh()
    return True, f"已移除群 {gid}，立即生效"


def add_teacher(user_id: str, operator: str = "", note: str = "") -> tuple[bool, str]:
    """添加老师。"""
    uid = (user_id or "").strip()
    if not uid:
        return False, "QQ 号不能为空"
    if uid in config.ENV_TEACHER_WHITELIST:
        return False, f"{uid} 已在配置文件中，无需重复添加"
    if not uid.isdigit():
        return False, "QQ 号应为纯数字"
    if not db.add_runtime_teacher(uid, operator=operator, note=note):
        return False, f"{uid} 已是老师"
    refresh()
    return True, f"已添加老师 {uid}，立即生效"


def remove_teacher(user_id: str, operator: str = "") -> tuple[bool, str]:
    """删除动态添加的老师。"""
    uid = (user_id or "").strip()
    if uid in config.ENV_TEACHER_WHITELIST:
        return False, f"{uid} 来自配置文件，请修改 ACM_TEACHER_WHITELIST 并重启"
    if not db.remove_runtime_teacher(uid, operator=operator):
        return False, f"{uid} 不在动态老师名单中"
    refresh()
    return True, f"已移除老师 {uid}，立即生效"


def describe() -> str:
    """给 /群列表 /老师列表 用的可读清单。"""
    lines = []

    env_groups = sorted(config.ENV_ALLOWED_GROUPS)
    dyn_groups = db.list_runtime_groups()
    lines.append(f"允许群：{len(config.ALLOWED_GROUPS)} 个")
    if env_groups:
        lines.append("  配置文件：" + "、".join(env_groups))
    if dyn_groups:
        lines.append("  动态添加：" + "、".join(
            f"{g['group_id']}" + (f"（{g['note']}）" if g["note"] else "")
            for g in dyn_groups
        ))
    if not env_groups and not dyn_groups:
        lines.append("  （暂无，机器人在任何群都不会响应）")

    env_teachers = sorted(config.ENV_TEACHER_WHITELIST)
    dyn_teachers = db.list_runtime_teachers()
    lines.append(f"老师：{len(config.TEACHER_WHITELIST)} 人")
    if env_teachers:
        lines.append("  配置文件：" + "、".join(env_teachers))
    if dyn_teachers:
        lines.append("  动态添加：" + "、".join(
            f"{t['user_id']}" + (f"（{t['note']}）" if t["note"] else "")
            for t in dyn_teachers
        ))
    if not env_teachers and not dyn_teachers:
        lines.append("  （暂无，所有老师命令都会被拒绝）")

    lines.append(f"超管：{len(config.SUPER_ADMIN)} 人（仅配置文件，不可运行时修改）")
    if config.SUPER_ADMIN:
        lines.append("  " + "、".join(sorted(config.SUPER_ADMIN)))
    return "\n".join(lines)
