"""超管私聊的「目标工作群（Active Group）」会话状态。

背景：超管若直接在官方大群里调试群配置（课程、FAQ、比赛状态、晨报），
每条指令与回执都会刷屏打扰群员。这里记录「某超管私聊当前绑定哪个群」，
私聊里的管理指令就按该群的单群视角执行，所有回执只走私聊，真实群无感知。

持久化：绑定关系落库到 ``admin_workspaces``（见 ``database.set_admin_workspace``），
进程重启后按 ``user_openid`` 自动恢复上次选定的工作区，不再退回「全局模式」，
避免重启后私聊管理误落到 ADMIN_GROUP（可能是测试群）。

语义约定：
- 内存字典是读缓存，命中即返回，未命中再回落到数据库；
- 显式 ``/重置群`` 会在库里留一条空记录，重启后**不会**再自动恢复，
  从而保证「清除绑定」是稳定生效的用户意图；
- 数据库不可用（未初始化、只读）时静默降级为纯内存，不影响主链路。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# admin_user_id -> 目标群 ID（group_id / group_openid）
_ACTIVE_GROUPS: dict[str, str] = {}


def _store():
    """延迟导入 database，避免模块导入期形成环。"""
    from cdutetc import database

    return database


def set_active_group(admin_user_id: str, group_id: str | None) -> None:
    """绑定超管私聊的目标群；传 None 等价于清除绑定（并落库显式清空标记）。"""
    uid = str(admin_user_id or "").strip()
    if not uid:
        return
    gid = str(group_id or "").strip()
    if not gid:
        clear_active_group(uid)
        return
    _ACTIVE_GROUPS[uid] = gid
    try:
        _store().set_admin_workspace(uid, gid)
    except Exception:  # noqa: BLE001 - 持久化失败不应打断命令链路
        logger.warning("工作区持久化失败，已降级为内存绑定 [admin_session]")


def get_active_group(admin_user_id: str) -> str | None:
    """返回该超管当前绑定的目标群；未绑定返回 None。

    内存未命中时回落到数据库，实现「服务重启后自动恢复上次工作区」。
    """
    uid = str(admin_user_id or "").strip()
    if not uid:
        return None
    cached = _ACTIVE_GROUPS.get(uid)
    if cached:
        return cached
    try:
        stored = _store().get_admin_workspace(uid)
    except Exception:  # noqa: BLE001 - 库未初始化等场景按未绑定处理
        return None
    if stored:
        _ACTIVE_GROUPS[uid] = stored
        return stored
    return None


def clear_active_group(admin_user_id: str) -> None:
    """清除该超管的绑定，私聊恢复全局管理模式（并写入显式清空标记）。"""
    uid = str(admin_user_id or "").strip()
    if not uid:
        return
    _ACTIVE_GROUPS.pop(uid, None)
    try:
        _store().clear_admin_workspace(uid)
    except Exception:  # noqa: BLE001 - 同上
        logger.warning("工作区清空未落库，仅内存生效 [admin_session]")


def reset_all() -> None:
    """清空内存缓存（测试与热重载用）；数据库中的工作区记录保持不变。"""
    _ACTIVE_GROUPS.clear()
