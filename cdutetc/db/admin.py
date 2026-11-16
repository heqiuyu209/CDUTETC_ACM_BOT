"""管理域数据访问：群白名单、教师授权、admin_workspaces、audit_log、group_settings、账号标注与别名。"""

import logging
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from cdutetc import config
from cdutetc.errors import DeliveryCode

from cdutetc.db import connection as _conn
from cdutetc.db import contests as _contests
from cdutetc.db import training as _training

logger = logging.getLogger(__name__)


def set_group_teachers(group_id: str, user_ids, operator: str = "") -> None:
    teachers = {_conn._as_str(uid) for uid in user_ids}
    if group_id not in config.ALLOWED_GROUPS:
        raise ValueError("群尚未授权")
    if not teachers.issubset(set(config.TEACHER_WHITELIST) | set(config.SUPER_ADMIN)):
        raise ValueError("包含未授权的老师，请先 /加老师，再登记负责群；本操作不会授予老师权限")
    with _conn._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM group_teacher_assignments WHERE group_id=?", (group_id,))
        conn.executemany("INSERT INTO group_teacher_assignments(group_id,user_id,updated_by,updated_at) VALUES (?,?,?,?)",
                         [(group_id, uid, operator, _conn.now_str()) for uid in sorted(teachers)])
        add_audit(operator, group_id, "set_group_teachers", "group", group_id, "", ",".join(sorted(teachers)), conn=conn)


def get_group_teachers(group_id: str) -> list[str]:
    with _conn._connect() as conn:
        rows = conn.execute("SELECT user_id FROM group_teacher_assignments WHERE group_id=? ORDER BY user_id", (group_id,)).fetchall()
    authorized = set(config.TEACHER_WHITELIST) | set(config.SUPER_ADMIN)
    return [row[0] for row in rows if row[0] in authorized]


def _publication_identity(info: dict, canonical_url: str) -> str:
    """精确身份：URL、名称、届次；无届次时再加入三个目标时间。

    不做名称模糊匹配，也不把缺失时间猜成某个已知场次。
    """
    parts = [canonical_url, info["name"], info["edition"]]
    if not info["edition"]:
        for field in ("official_reg_start", "official_reg_deadline", "official_time"):
            value = info[field]
            parts.append(datetime.strptime(value, "%Y-%m-%d %H:%M").strftime(
                "%Y-%m-%d %H:%M"
            ) if value else "")
    return json.dumps(parts, ensure_ascii=False, separators=(",", ":"))


def get_group_notification_block(protocol: str, group_id: str) -> dict | None:
    """按协议和群读取暂停状态；配额耗尽只阻断发生当天。"""
    with _conn._connect() as conn:
        row = conn.execute(
            "SELECT error_code, platform_code, blocked_at, retry_after FROM group_notification_blocks WHERE protocol=? AND group_id=?",
            (protocol, group_id),
        ).fetchone()
    if not row:
        return None
    block = dict(zip(("error_code", "platform_code", "blocked_at", "retry_after"), row))
    if block["error_code"] == DeliveryCode.QUOTA_EXHAUSTED.value:
        blocked_at = _training._delivery_parse_time(block["blocked_at"])
        now = _training._delivery_parse_time(_conn.now_str())
        if blocked_at and now and blocked_at.date() < now.date():
            return None
    return block


def _group_retry_time(block: dict) -> datetime:
    retry_after = _training._delivery_parse_time(block.get("retry_after") or "")
    if retry_after is not None:
        return retry_after
    blocked_at = _training._delivery_parse_time(block.get("blocked_at") or "") or _training._delivery_now()
    return blocked_at + timedelta(seconds=_contests._GROUP_PERMISSION_RETRY_SECONDS)


def group_notification_paused(protocol: str, group_id: str, now: datetime | None = None) -> bool:
    block = get_group_notification_block(protocol, group_id)
    return block is not None and (block["error_code"] != DeliveryCode.ACTIVE_FORBIDDEN.value
                                  or _training._delivery_now(now) < _group_retry_time(block))


def claim_group_notification_retry(protocol: str, group_id: str) -> bool:
    """跨任务/进程共用五分钟冷却；领取不解除暂停，成功回执才解除。"""
    current = _training._delivery_now()
    with _conn._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT error_code, blocked_at, retry_after FROM group_notification_blocks WHERE protocol=? AND group_id=?",
            (protocol, group_id),
        ).fetchone()
        if row is None:
            return True
        block = dict(zip(("error_code", "blocked_at", "retry_after"), row))
        if block["error_code"] == DeliveryCode.QUOTA_EXHAUSTED.value:
            blocked_at = _training._delivery_parse_time(block["blocked_at"])
            quota_now = _training._delivery_parse_time(_conn.now_str())
            return bool(blocked_at and quota_now and blocked_at.date() < quota_now.date())
        if block["error_code"] != DeliveryCode.ACTIVE_FORBIDDEN.value:
            return False
        if current < _group_retry_time(block):
            return False
        conn.execute(
            "UPDATE group_notification_blocks SET retry_after=? WHERE protocol=? AND group_id=?",
            ((current + timedelta(seconds=_contests._GROUP_PERMISSION_RETRY_SECONDS)).strftime("%Y-%m-%d %H:%M:%S"),
             protocol, group_id),
        )
        return True


def block_group_notifications(protocol: str, group_id: str, error_code: str, platform_code: int | None) -> None:
    retry_after = (_training._delivery_now() + timedelta(seconds=_contests._GROUP_PERMISSION_RETRY_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")
    with _conn._connect() as conn:
        conn.execute(
            "INSERT INTO group_notification_blocks (protocol, group_id, error_code, platform_code, blocked_at, retry_after)"
            " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(protocol, group_id) DO UPDATE SET"
            " error_code=excluded.error_code, platform_code=excluded.platform_code, blocked_at=excluded.blocked_at, retry_after=excluded.retry_after",
            (protocol, group_id, error_code, platform_code, _conn.now_str(), retry_after),
        )


def get_group_notification_format(protocol: str, group_id: str) -> str:
    with _conn._connect() as conn:
        row = conn.execute(
            "SELECT message_format FROM group_notification_settings WHERE protocol=? AND group_id=?",
            (protocol, group_id),
        ).fetchone()
    return row[0] if row else "text"


def confirm_group_notification_channel(protocol: str, group_id: str, message_format: str) -> None:
    """真实主动测试成功后，在同一事务记住格式并解除权限暂停。"""
    if message_format not in ("text", "markdown"):
        raise ValueError("通知格式无效")
    with _conn._connect() as conn:
        conn.execute(
            "INSERT INTO group_notification_settings (protocol, group_id, message_format, tested_at)"
            " VALUES (?, ?, ?, ?) ON CONFLICT(protocol, group_id) DO UPDATE SET"
            " message_format=excluded.message_format, tested_at=excluded.tested_at",
            (protocol, group_id, message_format, _conn.now_str()),
        )
        conn.execute("DELETE FROM group_notification_blocks WHERE protocol=? AND group_id=?", (protocol, group_id))


def set_group_setting(group_id: str, key: str, value: str, operator: str = "") -> None:
    """写入群级设置项（同一事务内记审计，便于回溯谁改了什么）。"""
    group_id = _conn._as_str(group_id)
    key = _conn._as_str(key)
    if not group_id or not key:
        raise ValueError("群设置需要群标识与设置项")
    value = _conn._as_str(value)
    with _conn._connect() as conn:
        conn.execute(
            "INSERT INTO group_settings (group_id, key, value, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(group_id, key) DO UPDATE SET"
            " value=excluded.value, updated_at=excluded.updated_at",
            (group_id, key, value, _conn.now_str()),
        )
        add_audit(operator, group_id, "set", "group_setting", key, "", value, conn=conn)


def get_group_setting(group_id: str, key: str, default: str = "") -> str:
    """读取群级设置项；未写入过返回 default。"""
    group_id = _conn._as_str(group_id)
    key = _conn._as_str(key)
    with _conn._connect() as conn:
        row = conn.execute(
            "SELECT value FROM group_settings WHERE group_id = ? AND key = ?",
            (group_id, key),
        ).fetchone()
    return row[0] if row and row[0] is not None else default


def set_group_course_remind(group_id: str, enabled: bool, operator: str = "") -> None:
    """课程提醒群级开关（默认开启，关闭后该群不再产生课程提醒）。"""
    if type(enabled) is not bool:
        raise ValueError("请明确开启或关闭课程提醒")
    set_group_setting(group_id, _training.COURSE_REMIND_SETTING,
                      _training._COURSE_REMIND_ON if enabled else _training._COURSE_REMIND_OFF, operator)


def group_course_remind_enabled(group_id: str) -> bool:
    """课程提醒是否开启；未设置过按默认 on 处理。

    库未初始化（缺 group_settings 表，如部分只建业务表的测试/旧库）时也按
    默认 on 处理：读不到开关不能让整条提醒链路中断。
    """
    try:
        value = get_group_setting(group_id, _training.COURSE_REMIND_SETTING, _training._COURSE_REMIND_ON)
    except sqlite3.OperationalError:
        return True
    return value.strip().lower() != _training._COURSE_REMIND_OFF


GROUP_SWITCH_KEYS = ("qa_enabled", "contest_remind_enabled",
                     "course_remind_enabled", "daily_digest_enabled")


GROUP_SWITCH_LABELS = {
    "qa_enabled": "问答",
    "contest_remind_enabled": "比赛提醒",
    "course_remind_enabled": "课程提醒",
    "daily_digest_enabled": "晨报",
}


_TRUE_VALUES = ("1", "true", "on", "yes", "开", "开启", "是")


_FALSE_VALUES = ("0", "false", "off", "no", "关", "关闭", "否")


def parse_switch_value(text: str) -> bool | None:
    """把 ``开/关/1/0/true/false`` 解析成布尔；无法识别返回 None。"""
    value = (text or "").strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    return None


def get_group_switches(group_id: str) -> dict[str, bool]:
    """读取某群四项后台开关；缺省行一律按开启处理（不返回 None）。"""
    switches = {key: True for key in GROUP_SWITCH_KEYS}
    group_id = _conn._as_str(group_id)
    try:
        with _conn._connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM group_settings WHERE group_id = ?", (group_id,)
            ).fetchall()
    except sqlite3.OperationalError:
        return switches
    for key, value in rows:
        if key in switches:
            parsed = parse_switch_value(value)
            if parsed is not None:
                switches[key] = parsed
    # 旧版「课程提醒」开关（course_remind=on/off）与新开关保持一致
    try:
        legacy = get_group_setting(group_id, _training.COURSE_REMIND_SETTING, "")
    except sqlite3.OperationalError:
        legacy = ""
    if legacy and "course_remind_enabled" not in {row[0] for row in rows}:
        switches["course_remind_enabled"] = legacy.strip().lower() != _training._COURSE_REMIND_OFF
    return switches


def set_group_switch(group_id: str, key: str, enabled: bool, operator: str = "") -> None:
    """写入单个后台开关；未知键直接报错，避免拼写错误静默失效。"""
    if key not in GROUP_SWITCH_KEYS:
        raise ValueError(f"未知开关：{key}")
    if type(enabled) is not bool:
        raise ValueError("请明确开启或关闭")
    set_group_setting(group_id, key, "on" if enabled else "off", operator)
    if key == "course_remind_enabled":
        # 同步旧键，保证 /群配置 课程提醒 与 /群开关 课程提醒 口径一致
        set_group_setting(group_id, _training.COURSE_REMIND_SETTING,
                          _training._COURSE_REMIND_ON if enabled else _training._COURSE_REMIND_OFF, operator)


def group_switch_enabled(group_id: str, key: str) -> bool:
    """单开关读取；库未初始化等异常一律按开启处理，不中断主链路。"""
    if key not in GROUP_SWITCH_KEYS:
        raise ValueError(f"未知开关：{key}")
    try:
        return get_group_switches(group_id)[key]
    except sqlite3.OperationalError:
        return True


_AUDIT_COLS = (
    "id, operator, group_id, action, object_type, object_id, before_json,"
    " after_json, created_at"
)


def _row_to_audit(r: sqlite3.Row) -> dict:
    return {
        "id": r[0], "operator": r[1], "group_id": r[2], "action": r[3],
        "object_type": r[4], "object_id": r[5], "before_json": r[6],
        "after_json": r[7], "created_at": r[8],
    }


def add_audit(operator: str, group_id: str, action: str, object_type: str,
              object_id: str, before: str = "", after: str = "",
              conn: sqlite3.Connection | None = None) -> None:
    """写入审计日志。业务调用方可在同一事务内传入 conn 保持同连接写库。

    默认（conn=None）自建连接，仅用于独立审计写入或兼容旧调用。
    """
    sql = (
        "INSERT INTO audit_log (operator, group_id, action, object_type, object_id,"
        " before_json, after_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    )
    params = (operator or "", group_id or "", action, object_type, object_id,
              before, after, _conn.now_str())
    if conn is not None:
        conn.execute(sql, params)
        return
    with _conn._connect() as conn:
        conn.execute(sql, params)


def list_audit(group_id: str | None = None, limit: int = 50) -> list[dict]:
    with _conn._connect() as conn:
        if group_id is None:
            rows = conn.execute(
                f"SELECT {_AUDIT_COLS} FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_AUDIT_COLS} FROM audit_log WHERE group_id = ? ORDER BY id DESC LIMIT ?",
                (group_id, limit),
            ).fetchall()
    return [_row_to_audit(r) for r in rows]


def list_runtime_groups() -> list[dict]:
    """列出运行时添加的允许群。"""
    with _conn._connect() as conn:
        rows = conn.execute(
            "SELECT group_id, note, added_by, added_at FROM runtime_allowed_groups"
            " ORDER BY added_at DESC"
        ).fetchall()
    return [
        {"group_id": r[0], "note": r[1], "added_by": r[2], "added_at": r[3]}
        for r in rows
    ]


def add_runtime_group(group_id: str, operator: str = "", note: str = "") -> bool:
    """添加运行时允许群；已存在返回 False。"""
    gid = _conn._as_str(group_id)
    if not gid:
        raise ValueError("群号不能为空")
    with _conn._connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO runtime_allowed_groups"
            " (group_id, note, added_by, added_at) VALUES (?, ?, ?, ?)",
            (gid, _conn._as_str(note or ""), _conn._as_str(operator or ""), _conn.now_str()),
        )
        if cur.rowcount:
            add_audit(operator, gid, "add_group", "runtime_whitelist", gid,
                      "", note or "", conn=conn)
        return bool(cur.rowcount)


def remove_runtime_group(group_id: str, operator: str = "") -> bool:
    """删除运行时允许群（不影响 .env 基线）。"""
    gid = _conn._as_str(group_id)
    with _conn._connect() as conn:
        cur = conn.execute(
            "DELETE FROM runtime_allowed_groups WHERE group_id = ?", (gid,)
        )
        if cur.rowcount:
            add_audit(operator, gid, "remove_group", "runtime_whitelist", gid,
                      "", "", conn=conn)
        return bool(cur.rowcount)


def list_runtime_teachers() -> list[dict]:
    """列出运行时添加的老师。"""
    with _conn._connect() as conn:
        rows = conn.execute(
            "SELECT user_id, note, added_by, added_at FROM runtime_teachers"
            " ORDER BY added_at DESC"
        ).fetchall()
    return [
        {"user_id": r[0], "note": r[1], "added_by": r[2], "added_at": r[3]}
        for r in rows
    ]


def add_runtime_teacher(user_id: str, operator: str = "", note: str = "") -> bool:
    """添加运行时老师；已存在返回 False。"""
    uid = _conn._as_str(user_id)
    if not uid:
        raise ValueError("QQ 号不能为空")
    with _conn._connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO runtime_teachers"
            " (user_id, note, added_by, added_at) VALUES (?, ?, ?, ?)",
            (uid, _conn._as_str(note or ""), _conn._as_str(operator or ""), _conn.now_str()),
        )
        if cur.rowcount:
            add_audit(operator, "", "add_teacher", "runtime_whitelist", uid,
                      "", note or "", conn=conn)
        return bool(cur.rowcount)


def remove_runtime_teacher(user_id: str, operator: str = "") -> bool:
    """删除运行时老师（不影响 .env 基线）。"""
    uid = _conn._as_str(user_id)
    with _conn._connect() as conn:
        cur = conn.execute(
            "DELETE FROM runtime_teachers WHERE user_id = ?", (uid,)
        )
        if cur.rowcount:
            add_audit(operator, "", "remove_teacher", "runtime_whitelist", uid,
                      "", "", conn=conn)
        return bool(cur.rowcount)


_LABEL_KINDS = ("group", "user")


def set_account_label(kind: str, account_id: str, label: str,
                      operator: str = "") -> bool:
    """给群/人设置可读别名。kind 取 group 或 user。"""
    kind = _conn._as_str(kind).lower()
    account_id = _conn._as_str(account_id)
    label = _conn._as_str(label)
    if kind not in _LABEL_KINDS:
        raise ValueError("别名类型只能是 group 或 user")
    if not account_id:
        raise ValueError("标识不能为空")
    if not label:
        raise ValueError("名称不能为空")
    with _conn._connect() as conn:
        conn.execute(
            "INSERT INTO account_labels (kind, account_id, label, updated_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(kind, account_id) DO UPDATE SET"
            " label = excluded.label, updated_at = excluded.updated_at",
            (kind, account_id, label, _conn.now_str()),
        )
        add_audit(operator, account_id if kind == "group" else "", "set_label",
                  kind, account_id, "", label, conn=conn)
    return True


def get_account_label(kind: str, account_id: str) -> str:
    """取别名；没有则返回空串。"""
    with _conn._connect() as conn:
        row = conn.execute(
            "SELECT label FROM account_labels WHERE kind = ? AND account_id = ?",
            (_conn._as_str(kind).lower(), _conn._as_str(account_id)),
        ).fetchone()
    return row[0] if row else ""


def remove_account_label(kind: str, account_id: str, operator: str = "") -> bool:
    with _conn._connect() as conn:
        cur = conn.execute(
            "DELETE FROM account_labels WHERE kind = ? AND account_id = ?",
            (_conn._as_str(kind).lower(), _conn._as_str(account_id)),
        )
        if cur.rowcount:
            add_audit(operator, "", "remove_label", kind, account_id, "", "",
                      conn=conn)
        return bool(cur.rowcount)


def list_account_labels(kind: str | None = None) -> list[dict]:
    with _conn._connect() as conn:
        if kind:
            rows = conn.execute(
                "SELECT kind, account_id, label, updated_at FROM account_labels"
                " WHERE kind = ? ORDER BY updated_at DESC", (_conn._as_str(kind).lower(),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT kind, account_id, label, updated_at FROM account_labels"
                " ORDER BY kind, updated_at DESC"
            ).fetchall()
    return [
        {"kind": r[0], "account_id": r[1], "label": r[2], "updated_at": r[3]}
        for r in rows
    ]


def describe_account(kind: str, account_id: str) -> str:
    """把标识渲染成「名字（前8位…后4位）」；没有别名时只显示缩写。"""
    account_id = _conn._as_str(account_id)
    label = get_account_label(kind, account_id)
    if len(account_id) > 14:
        short = f"{account_id[:8]}…{account_id[-4:]}"
    else:
        short = account_id
    return f"{label}（{short}）" if label else short


_ALIAS_ENTITY_TYPES = ("group", "user")


def _normalize_entity_type(entity_type: str) -> str:
    value = _conn._as_str(entity_type).lower()
    if value not in _ALIAS_ENTITY_TYPES:
        raise ValueError("别名类型只能是 group 或 user")
    return value


def set_entity_alias(entity_type: str, openid: str, display_name: str,
                     numeric_id: str = "", operator: str = "") -> bool:
    """绑定 openid 与真实群号/QQ号、备注名。

    同时镜像一份名字到 account_labels，让既有「命名群/命名人」相关渲染
    与按名字匹配的旧逻辑无需改动即可共用同一个名字。
    """
    entity_type = _normalize_entity_type(entity_type)
    openid = _conn._as_str(openid)
    display_name = _conn._as_str(display_name)
    numeric_id = _conn._as_str(numeric_id)
    if not openid:
        raise ValueError("标识不能为空")
    if not display_name:
        raise ValueError("名称不能为空")
    with _conn._connect() as conn:
        conn.execute(
            "INSERT INTO entity_aliases (entity_type, openid, numeric_id, display_name, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(entity_type, openid) DO UPDATE SET"
            " numeric_id = excluded.numeric_id,"
            " display_name = excluded.display_name,"
            " updated_at = excluded.updated_at",
            (entity_type, openid, numeric_id, display_name, _conn.now_str()),
        )
        conn.execute(
            "INSERT INTO account_labels (kind, account_id, label, updated_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(kind, account_id) DO UPDATE SET"
            " label = excluded.label, updated_at = excluded.updated_at",
            (entity_type, openid, display_name, _conn.now_str()),
        )
        add_audit(operator, openid if entity_type == "group" else "", "set_alias",
                  entity_type, openid, "", f"{display_name}|{numeric_id}", conn=conn)
    return True


def get_entity_alias(entity_type: str, openid: str) -> dict | None:
    """取别名记录；没有绑定返回 None（调用方负责降级成短哈希展示）。"""
    entity_type = _normalize_entity_type(entity_type)
    openid = _conn._as_str(openid)
    if not openid:
        return None
    with _conn._connect() as conn:
        row = conn.execute(
            "SELECT entity_type, openid, numeric_id, display_name, updated_at"
            " FROM entity_aliases WHERE entity_type = ? AND openid = ?",
            (entity_type, openid),
        ).fetchone()
    if not row:
        return None
    return {"entity_type": row[0], "openid": row[1], "numeric_id": row[2],
            "display_name": row[3], "updated_at": row[4]}


def list_entity_aliases(entity_type: str | None = None) -> list[dict]:
    with _conn._connect() as conn:
        if entity_type:
            kind = _normalize_entity_type(entity_type)
            rows = conn.execute(
                "SELECT entity_type, openid, numeric_id, display_name, updated_at"
                " FROM entity_aliases WHERE entity_type = ? ORDER BY updated_at DESC",
                (kind,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT entity_type, openid, numeric_id, display_name, updated_at"
                " FROM entity_aliases ORDER BY entity_type, updated_at DESC"
            ).fetchall()
    return [
        {"entity_type": r[0], "openid": r[1], "numeric_id": r[2],
         "display_name": r[3], "updated_at": r[4]}
        for r in rows
    ]


def list_group_aliases() -> dict[str, dict]:
    """{group_openid: {"display_name":…, "numeric_id":…, "updated_at":…}}"""
    return {
        item["openid"]: {"display_name": item["display_name"],
                         "numeric_id": item["numeric_id"],
                         "updated_at": item["updated_at"]}
        for item in list_entity_aliases("group")
    }


def remove_entity_alias(entity_type: str, openid: str, operator: str = "") -> bool:
    entity_type = _normalize_entity_type(entity_type)
    openid = _conn._as_str(openid)
    with _conn._connect() as conn:
        cur = conn.execute(
            "DELETE FROM entity_aliases WHERE entity_type = ? AND openid = ?",
            (entity_type, openid),
        )
        if cur.rowcount:
            add_audit(operator, openid if entity_type == "group" else "",
                      "remove_alias", entity_type, openid, "", "", conn=conn)
        return bool(cur.rowcount)


def set_admin_workspace(user_openid: str, group_openid: str) -> None:
    """写入/覆盖某超管的工作区；传空串表示「显式清空」而非删除记录。"""
    uid = _conn._as_str(user_openid)
    if not uid:
        raise ValueError("超管标识不能为空")
    gid = _conn._as_str(group_openid)
    with _conn._connect() as conn:
        conn.execute(
            "INSERT INTO admin_workspaces (user_openid, group_openid, updated_at)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(user_openid) DO UPDATE SET group_openid = excluded.group_openid,"
            " updated_at = excluded.updated_at",
            (uid, gid, _conn.now_str()),
        )


def get_admin_workspace(user_openid: str) -> str | None:
    """返回该超管上次锁定的工作区；无记录或已被显式清空都返回 None。"""
    uid = _conn._as_str(user_openid)
    if not uid:
        return None
    with _conn._connect() as conn:
        row = conn.execute(
            "SELECT group_openid FROM admin_workspaces WHERE user_openid = ?", (uid,)
        ).fetchone()
    if row is None or not (row[0] or "").strip():
        return None
    return row[0].strip()


def has_admin_workspace(user_openid: str) -> bool:
    """该超管是否有过工作区记录（含「显式清空」的空记录）。"""
    uid = _conn._as_str(user_openid)
    if not uid:
        return False
    with _conn._connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM admin_workspaces WHERE user_openid = ?", (uid,)
        ).fetchone()
    return row is not None


def clear_admin_workspace(user_openid: str) -> None:
    """显式清空：保留一条空记录，防止重启后又自动回落到默认群。"""
    set_admin_workspace(user_openid, "")


_TEACHER_INTENT_COLS = (
    "id, group_id, sender_id, msg_id, raw_text, intent, slots_json, missing,"
    " status, resolved_by, created_at, updated_at"
)


def _row_to_teacher_intent(row: sqlite3.Row) -> dict:
    try:
        slots = json.loads(row[6] or "{}")
    except ValueError:
        slots = {}
    return {
        "id": row[0], "group_id": row[1], "sender_id": row[2], "msg_id": row[3],
        "raw_text": row[4], "intent": row[5],
        "slots": slots if isinstance(slots, dict) else {},
        "missing": [item for item in (row[7] or "").split(",") if item],
        "status": row[8], "resolved_by": row[9],
        "created_at": row[10], "updated_at": row[11],
    }


def add_teacher_intent_pending(group_id: str, *, sender_id: str = "", msg_id: str = "",
                               raw_text: str = "", intent: str = "",
                               slots: dict | None = None,
                               missing: list[str] | None = None) -> int:
    """记录一条「时间/事项不完整」的老师通知，等待补全后转正式安排。

    同一 (群, 消息) 只留一条：平台重推同一条消息时更新原记录而不是堆积重复待办。
    """
    gid = _conn._as_str(group_id)
    if not gid:
        raise ValueError("群标识不能为空")
    payload = json.dumps(slots or {}, ensure_ascii=False, sort_keys=True)
    gaps = ",".join(item for item in (missing or []) if item)
    stamp = _conn.now_str()
    # UNIQUE(group_id, msg_id) 要求键非空且稳定：取不到入站 msg_id 时（如本地补录）
    # 退化为正文摘要，保证同一段话重复进来是更新而不是堆一条新待办。
    key = _conn._as_str(msg_id) or "text:" + hashlib.sha1(
        _conn._as_str(raw_text).encode("utf-8")).hexdigest()[:16]
    with _conn._connect() as conn:
        conn.execute(
            "INSERT INTO teacher_intent_pending (group_id, sender_id, msg_id, raw_text,"
            " intent, slots_json, missing, status, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,'pending',?,?)"
            " ON CONFLICT(group_id, msg_id) DO UPDATE SET"
            " sender_id = excluded.sender_id, raw_text = excluded.raw_text,"
            " intent = excluded.intent, slots_json = excluded.slots_json,"
            " missing = excluded.missing, status = 'pending', updated_at = excluded.updated_at",
            (gid, _conn._as_str(sender_id), key, _conn._as_str(raw_text), _conn._as_str(intent),
             payload, gaps, stamp, stamp),
        )
        row = conn.execute(
            "SELECT id FROM teacher_intent_pending WHERE group_id = ? AND msg_id = ?",
            (gid, key),
        ).fetchone()
    return int(row[0]) if row else 0


def get_teacher_intent_pending(pid: int) -> dict | None:
    """按 id 读取一条待补全条目。"""
    try:
        key = int(pid)
    except (TypeError, ValueError):
        return None
    with _conn._connect() as conn:
        row = conn.execute(
            f"SELECT {_TEACHER_INTENT_COLS} FROM teacher_intent_pending WHERE id = ?",
            (key,),
        ).fetchone()
    return _row_to_teacher_intent(row) if row else None


def list_teacher_intent_pending(group_id: str = "", status: str = "pending") -> list[dict]:
    """列出待补全条目；group_id 留空表示全部群。"""
    where, args = [], []
    if _conn._as_str(group_id):
        where.append("group_id = ?")
        args.append(_conn._as_str(group_id))
    if status:
        where.append("status = ?")
        args.append(_conn._as_str(status))
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    with _conn._connect() as conn:
        rows = conn.execute(
            f"SELECT {_TEACHER_INTENT_COLS} FROM teacher_intent_pending{clause}"
            " ORDER BY id DESC",
            args,
        ).fetchall()
    return [_row_to_teacher_intent(r) for r in rows]


def resolve_teacher_intent_pending(pid: int, status: str = "resolved",
                                   operator: str = "") -> bool:
    """关闭待补全条目（resolved=已补全 / dropped=放弃）；不物理删除。"""
    if status not in ("resolved", "dropped"):
        raise ValueError("状态必须是 resolved 或 dropped")
    try:
        key = int(pid)
    except (TypeError, ValueError):
        return False
    with _conn._connect() as conn:
        cur = conn.execute(
            "UPDATE teacher_intent_pending SET status = ?, resolved_by = ?, updated_at = ?"
            " WHERE id = ? AND status = 'pending'",
            (status, _conn._as_str(operator), _conn.now_str(), key),
        )
        return cur.rowcount > 0
