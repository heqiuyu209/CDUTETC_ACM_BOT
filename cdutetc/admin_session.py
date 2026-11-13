"""超管私聊的「目标工作群（Active Group）」会话状态。

背景：超管若直接在官方大群里调试群配置（课程、FAQ、比赛状态、晨报），
每条指令与回执都会刷屏打扰群员。这里在内存中记录「某超管私聊当前绑定哪个群」，
私聊里的管理指令就按该群的单群视角执行，所有回执只走私聊，真实群无感知。

设计约束：
- 仅内存字典，进程重启即丢失（重启后退回全局管理模式，属于可接受的降级）；
- 只服务超管私聊，不落地数据库、不做持久化，避免误绑定长期生效；
- 线程/协程安全：仅做单键读写，CPython 下 dict 操作原子，无需加锁。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# admin_user_id -> 目标群 ID（group_id / group_openid）
_ACTIVE_GROUPS: dict[str, str] = {}


def set_active_group(admin_user_id: str, group_id: str | None) -> None:
    """绑定超管私聊的目标群；传 None 等价于清除绑定。"""
    uid = str(admin_user_id or "").strip()
    if not uid:
        return
    gid = str(group_id or "").strip()
    if not gid:
        clear_active_group(uid)
        return
    _ACTIVE_GROUPS[uid] = gid


def get_active_group(admin_user_id: str) -> str | None:
    """返回该超管当前绑定的目标群；未绑定返回 None。"""
    uid = str(admin_user_id or "").strip()
    if not uid:
        return None
    return _ACTIVE_GROUPS.get(uid)


def clear_active_group(admin_user_id: str) -> None:
    """清除该超管的绑定，私聊恢复全局管理模式。"""
    uid = str(admin_user_id or "").strip()
    if uid:
        _ACTIVE_GROUPS.pop(uid, None)


def reset_all() -> None:
    """清空全部绑定（测试与热重载用）。"""
    _ACTIVE_GROUPS.clear()
