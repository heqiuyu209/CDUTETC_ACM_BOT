"""训练与提醒域数据访问：training_sessions、org_events、reminder_deliveries 租约投递、自定义提醒。"""

import logging
import json
import sqlite3
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta
from cdutetc import config

from cdutetc.db import admin as _admin
from cdutetc.db import connection as _conn
from cdutetc.db import contests as _contests

logger = logging.getLogger(__name__)


_ORG_EVENT_COLS = (
    "id, title, event_type, reg_deadline, contest_time, location, teacher,"
    " note, source, status, shareable, effective_start, effective_end,"
    " created_at, updated_at"
)


def _row_to_org_event(r: sqlite3.Row) -> dict:
    return {
        "id": r[0], "title": r[1], "event_type": r[2], "reg_deadline": r[3],
        "contest_time": r[4], "location": r[5], "teacher": r[6], "note": r[7],
        "source": r[8], "status": r[9], "shareable": r[10],
        "effective_start": r[11], "effective_end": r[12],
        "created_at": r[13], "updated_at": r[14],
    }


def create_org_event(info: dict, operator: str = "") -> int:
    """创建学校/集训队统一安排。title 必填，时间字段统一格式。"""
    title = _conn._as_str(info.get("title"))
    if not title:
        raise ValueError("安排标题不能为空")
    values = (
        title,
        _conn._as_str(info.get("event_type") or ""),
        _conn._clean_time(info.get("reg_deadline")),
        _conn._clean_time(info.get("contest_time")),
        _conn._as_str(info.get("location") or ""),
        _conn._as_str(info.get("teacher") or ""),
        _conn._as_str(info.get("note") or ""),
        _conn._as_str(info.get("source") or ""),
        _conn._as_str(info.get("status") or "draft"),
        1 if info.get("shareable") else 0,
        _conn._clean_time(info.get("effective_start")),
        _conn._clean_time(info.get("effective_end")),
        _conn.now_str(), _conn.now_str(),
    )
    if values[8] not in ("draft", "confirmed"):
        raise ValueError("状态必须是 draft 或 confirmed")
    with _conn._connect() as conn:
        cur = conn.execute(
            "INSERT INTO org_events (title, event_type, reg_deadline, contest_time,"
            " location, teacher, note, source, status, shareable, effective_start,"
            " effective_end, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
        _admin.add_audit(operator, "", "create", "org_event", str(cur.lastrowid), "",
                  json.dumps(values, ensure_ascii=False), conn=conn)
        return cur.lastrowid


def list_org_events(status: str | None = None, shareable_only: bool = False) -> list[dict]:
    with _conn._connect() as conn:
        clauses = []
        args: list = []
        if status is not None:
            clauses.append("status = ?")
            args.append(status)
        if shareable_only:
            clauses.append("shareable = 1")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(
            f"SELECT {_ORG_EVENT_COLS} FROM org_events{where} ORDER BY id DESC", args
        ).fetchall()
    return [_row_to_org_event(r) for r in rows]


def confirm_org_event(eid: int, operator: str = "") -> bool:
    with _conn._connect() as conn:
        cur = conn.execute(
            "UPDATE org_events SET status = 'confirmed', updated_at = ? WHERE id = ?",
            (_conn.now_str(), eid),
        )
        if cur.rowcount:
            _admin.add_audit(operator, "", "confirm", "org_event", str(eid), "",
                      "confirmed", conn=conn)
        return cur.rowcount > 0


def update_org_event(eid: int, info: dict, operator: str = "") -> bool:
    """更新学校安排字段（仅传入字段）。"""
    mapping = {
        "title": lambda v: (_conn._as_str(v),),
        "event_type": lambda v: (_conn._as_str(v or ""),),
        "reg_deadline": lambda v: (_conn._clean_time(v),),
        "contest_time": lambda v: (_conn._clean_time(v),),
        "location": lambda v: (_conn._as_str(v or ""),),
        "teacher": lambda v: (_conn._as_str(v or ""),),
        "note": lambda v: (_conn._as_str(v or ""),),
        "source": lambda v: (_conn._as_str(v or ""),),
        "shareable": lambda v: (1 if v else 0,),
    }
    sets = []
    args: list = []
    for field, fn in mapping.items():
        if field in info:
            sets.append(f"{field} = ?")
            args.extend(fn(info[field]))
    if not sets:
        return False
    args += [_conn.now_str(), eid]
    with _conn._connect() as conn:
        cur = conn.execute(
            f"UPDATE org_events SET {', '.join(sets)}, updated_at = ? WHERE id = ?", args
        )
        if cur.rowcount:
            _admin.add_audit(operator, "", "update", "org_event", str(eid), "",
                      json.dumps(info, ensure_ascii=False), conn=conn)
        return cur.rowcount > 0


_TRAINING_COLS = (
    "id, group_id, title, weekday, start_time, end_time, location, needs_laptop,"
    " software, device_req, training_group, effective_start, effective_end,"
    " status, source, maintainer, updated_at, requirement"
)


def _training_weekday(text: str) -> int | None:
    names = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
    text = (text or "").strip()
    if text.startswith("每"):
        text = text[1:]
    for prefix in ("星期", "礼拜", "周"):
        if text.startswith(prefix):
            return names.get(text[len(prefix):])
    return None


def _training_start_clock(text: str):
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).time()
        except (ValueError, TypeError):
            continue
    return None


def training_occurrence_valid(session: dict, target: datetime) -> bool:
    """当前课程是否仍包含该具体开课实例；生效边界比较实例时间而非扫描时间。"""
    target = _delivery_now(target)
    clock = _training_start_clock(session.get("start_time"))
    if (session.get("status") != "confirmed" or clock is None
            or target.weekday() != _training_weekday(session.get("weekday"))
            or target.time() != clock):
        return False
    for field, is_start in (("effective_start", True), ("effective_end", False)):
        value = session.get(field) or ""
        if not value:
            continue
        bound = _delivery_parse_time(value)
        if bound is None or (target < bound if is_start else target > bound):
            return False
    return True


def training_occurrence_times(session: dict, now: datetime, before_hours: float) -> list[datetime]:
    """展开提醒窗口内尚未开始的每周课程，未知星期/时间不产生猜测计划。"""
    now = _delivery_now(now)
    weekday = _training_weekday(session.get("weekday"))
    clock = _training_start_clock(session.get("start_time"))
    if weekday is None or clock is None or before_hours <= 0:
        return []
    day = now.date() + timedelta(days=(weekday - now.weekday()) % 7)
    target = datetime.combine(day, clock, tzinfo=_conn.TZ)
    if target <= now:
        target += timedelta(days=7)
    until = now + timedelta(hours=before_hours)
    out = []
    while target <= until:
        if training_occurrence_valid(session, target):
            out.append(target)
        target += timedelta(days=7)
    return out


def _row_to_training(r: sqlite3.Row) -> dict:
    return {
        "id": r[0], "group_id": r[1], "title": r[2], "weekday": r[3],
        "start_time": r[4], "end_time": r[5], "location": r[6],
        "needs_laptop": r[7], "software": r[8], "device_req": r[9],
        "training_group": r[10], "effective_start": r[11], "effective_end": r[12],
        "status": r[13], "source": r[14], "maintainer": r[15], "updated_at": r[16],
        "requirement": r[17],
    }


def create_training_session(group_id: str, info: dict, operator: str = "") -> int:
    """创建群内课程/训练安排。group_id 必填。"""
    group_id = _conn._as_str(group_id)
    title = _conn._as_str(info.get("title") or "")
    if not title:
        title = f"训练安排 {_conn.now_str()[:10]}"
    values = (
        group_id, title,
        _conn._as_str(info.get("weekday") or ""),
        _conn._as_str(info.get("start_time") or ""),
        _conn._as_str(info.get("end_time") or ""),
        _conn._as_str(info.get("location") or ""),
        1 if info.get("needs_laptop") else 0,
        _conn._as_str(info.get("software") or ""),
        _conn._as_str(info.get("device_req") or ""),
        _conn._as_str(info.get("training_group") or ""),
        _conn._clean_time(info.get("effective_start")),
        _conn._clean_time(info.get("effective_end")),
        _conn._as_str(info.get("status") or "confirmed"),
        _conn._as_str(info.get("source") or ""),
        _conn._as_str(operator or ""),
        _conn.now_str(),
        _conn._as_str(info.get("requirement") or ""),
    )
    if values[12] not in ("confirmed", "draft", "cancelled"):
        raise ValueError("状态必须是 confirmed/draft/cancelled")
    with _conn._connect() as conn:
        cur = conn.execute(
            "INSERT INTO training_sessions (group_id, title, weekday, start_time,"
            " end_time, location, needs_laptop, software, device_req, training_group,"
            " effective_start, effective_end, status, source, maintainer, updated_at,"
            " requirement)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
        return cur.lastrowid


def list_training_sessions(group_id: str, active_only: bool = True,
                           include_cancelled: bool = False) -> list[dict]:
    """列出某群课程安排。active_only：仅生效期内且未取消。"""
    with _conn._connect() as conn:
        where = "group_id = ?"
        args: list = [group_id]
        if active_only:
            now = _conn.now_str()
            where += (" AND status = 'confirmed'"
                      " AND (effective_start = '' OR effective_start <= ?)"
                      " AND (effective_end = '' OR effective_end >= ?)")
            args += [now, now]
        elif not include_cancelled:
            where += " AND status != 'cancelled'"
        rows = conn.execute(
            f"SELECT {_TRAINING_COLS} FROM training_sessions WHERE {where} ORDER BY id DESC",
            args,
        ).fetchall()
    return [_row_to_training(r) for r in rows]


def cancel_training_session(group_id: str, tid: int, operator: str = "") -> bool:
    """取消课程（标记 cancelled，不物理删除；旧安排不再视为当前安排）。"""
    with _conn._connect() as conn:
        cur = conn.execute(
            "UPDATE training_sessions SET status = 'cancelled', updated_at = ?"
            " WHERE id = ? AND group_id = ?",
            (_conn.now_str(), tid, group_id),
        )
        if cur.rowcount:
            _admin.add_audit(operator, group_id, "cancel", "training_session", str(tid), "",
                      "cancelled", conn=conn)
        return cur.rowcount > 0


_TRAINING_UPDATE_FIELDS = (
    "title", "weekday", "start_time", "end_time", "location", "needs_laptop",
    "software", "device_req", "training_group", "effective_start", "effective_end",
    "status", "source", "requirement",
)


_TRAINING_TIME_FIELDS = ("effective_start", "effective_end")


_TRAINING_STATUSES = ("confirmed", "draft", "cancelled")


def get_group_course(group_id: str, course_id: int) -> dict | None:
    """按群 + 课程 id 读取单条课程；群不匹配视为不存在（不跨群泄露）。"""
    try:
        cid = int(course_id)
    except (TypeError, ValueError):
        return None
    with _conn._connect() as conn:
        row = conn.execute(
            f"SELECT {_TRAINING_COLS} FROM training_sessions WHERE id = ? AND group_id = ?",
            (cid, _conn._as_str(group_id)),
        ).fetchone()
    return _row_to_training(row) if row else None


def update_group_course(group_id: str, course_id: int, operator: str = "", **kwargs) -> bool:
    """修改课程的可编辑字段（白名单字段 + 审计留痕）。

    - 未知字段直接 ValueError，避免拼写错误静默丢改动；
    - 只更新显式传入且非 None 的字段，其余保持原值；
    - 审计 before 记空，after 记本次变更字段名列表，与既有 update 语义一致。
    """
    group_id = _conn._as_str(group_id)
    try:
        cid = int(course_id)
    except (TypeError, ValueError):
        return False
    unknown = sorted(set(kwargs) - set(_TRAINING_UPDATE_FIELDS))
    if unknown:
        raise ValueError(f"不支持修改的字段：{'、'.join(unknown)}")
    changes: dict[str, object] = {}
    for field, value in kwargs.items():
        if value is None:
            continue
        if field == "title":
            title = _conn._as_str(value).strip()
            if not title:
                raise ValueError("课程标题不能为空")
            changes[field] = title
        elif field == "status":
            status = _conn._as_str(value)
            if status not in _TRAINING_STATUSES:
                raise ValueError("状态必须是 confirmed/draft/cancelled")
            changes[field] = status
        elif field == "needs_laptop":
            changes[field] = 1 if value else 0
        elif field in _TRAINING_TIME_FIELDS:
            changes[field] = _conn._clean_time(value)
        else:
            changes[field] = _conn._as_str(value)
    if not changes:
        return False
    assignments = ", ".join(f"{field} = ?" for field in changes)
    values = [*changes.values(), _conn.now_str(), cid, group_id]
    with _conn._connect() as conn:
        cur = conn.execute(
            f"UPDATE training_sessions SET {assignments}, updated_at = ?"
            " WHERE id = ? AND group_id = ?",
            values,
        )
        if cur.rowcount:
            _admin.add_audit(operator, group_id, "update", "training_session", str(cid), "",
                      json.dumps(sorted(changes), ensure_ascii=False), conn=conn)
        return cur.rowcount > 0


def sync_future_contest_start_reminders(now: datetime | None = None) -> None:
    """启动和授权变化后补齐未来计划；不补过去节点、不重置发送历史。"""
    current = _delivery_now(now)
    with _conn._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT id FROM competitions WHERE status='confirmed' AND official_time>=?",
            (current.strftime("%Y-%m-%d %H:%M"),),
        ).fetchall()
        for (cid,) in rows:
            _sync_contest_start_reminders(conn, cid, now=current)


def _sync_contest_start_reminders(conn: sqlite3.Connection, competition_id: int,
                                  group_ids=None, *, now: datetime | None = None,
                                  only_group: str | None = None) -> None:
    """在业务写事务中同步比赛开赛前节点；过去节点不创建、不补发。"""
    row = conn.execute(
        "SELECT status, official_time FROM competitions WHERE id = ?", (competition_id,)
    ).fetchone()
    if row is None:
        # 比赛记录已不存在（物理清理或被替换）：残留的待发节点必须一并取消，
        # 否则开赛前 15 分钟会推一条指向不存在比赛的提醒。
        conn.execute(
            "UPDATE reminder_deliveries SET status = 'cancelled', lease_token = '', lease_until = ''"
            " WHERE obj_type = 'competition' AND obj_id = ? AND remind_type = 'contest_start'"
            " AND status IN ('pending', 'sending')"
            + (" AND group_id=?" if only_group is not None else ""),
            (competition_id, only_group) if only_group is not None else (competition_id,),
        )
        return
    offsets = tuple(getattr(config, "CONTEST_REMIND_OFFSETS_MINUTES", (30, 15)))
    try:
        grace_seconds = max(1, int(getattr(config, "CONTEST_REMIND_GRACE_SECONDS", 120)))
    except (TypeError, ValueError):
        grace_seconds = 120
    target = _delivery_parse_time(row[1] or "")
    groups = set()
    if row[0] == "confirmed" and target is not None:
        groups = _contests._competition_target_groups_on(conn, competition_id)
        if group_ids is not None:
            groups &= {str(g).strip() for g in group_ids if str(g).strip()}
    current = _delivery_now(now)
    desired: set[str] = set()
    if target is not None and row[0] == "confirmed":
        for group_id in sorted(groups):
            for raw_offset in offsets:
                try:
                    minutes = int(raw_offset)
                except (TypeError, ValueError):
                    continue
                if minutes <= 0:
                    continue
                scheduled = target - timedelta(minutes=minutes)
                expires = min(scheduled + timedelta(seconds=grace_seconds), target)
                key = delivery_dedup_key(
                    group_id, "competition", competition_id, "contest_start",
                    row[1], minutes / 60,
                )
                # Existing in-grace deliveries remain desired, but never create a late plan.
                existing = conn.execute(
                    "SELECT id, status, attempts FROM reminder_deliveries WHERE dedup_key = ?", (key,)
                ).fetchone()
                if expires >= current:
                    desired.add(key)
                if scheduled > current and existing is None:
                    conn.execute(
                        "INSERT INTO reminder_deliveries"
                        " (group_id, obj_type, obj_id, remind_type, target_time, status, dedup_key, scheduled_at, expires_at)"
                        " VALUES (?, 'competition', ?, 'contest_start', ?, 'pending', ?, ?, ?)",
                        (group_id, competition_id, _conn._canonical_time(row[1]), key,
                         scheduled.strftime("%Y-%m-%d %H:%M:%S"), expires.strftime("%Y-%m-%d %H:%M:%S")),
                    )
                elif (scheduled > current and existing is not None
                      and existing[1] == "cancelled" and existing[2] == 0
                      and not conn.execute(
                          "SELECT 1 FROM reminder_success_receipts WHERE dedup_key = ?", (key,)
                      ).fetchone()):
                    conn.execute(
                        "UPDATE reminder_deliveries SET status='pending', last_error='', error_code=''"
                        " WHERE id=? AND status='cancelled' AND attempts=0", (existing[0],)
                    )
    old = conn.execute(
        "SELECT id, dedup_key, status FROM reminder_deliveries"
        " WHERE obj_type = 'competition' AND obj_id = ? AND remind_type = 'contest_start'"
        " AND status IN ('pending', 'sending')"
        + (" AND group_id=?" if only_group is not None else ""),
        (competition_id, only_group) if only_group is not None else (competition_id,),
    ).fetchall()
    for delivery_id, key, status in old:
        if key not in desired:
            conn.execute(
                "UPDATE reminder_deliveries SET status = 'cancelled', lease_token = '', lease_until = ''"
                " WHERE id = ?", (delivery_id,),
            )


_DELIVERY_COLS = (
    "id, group_id, obj_type, obj_id, remind_type, target_time, attempts,"
    " status, sent_at, last_error, dedup_key, lease_token, lease_until,"
    " scheduled_at, expires_at, error_code, object_snapshot"
)


def _row_to_delivery(r: sqlite3.Row) -> dict:
    return {
        "id": r[0], "group_id": r[1], "obj_type": r[2], "obj_id": r[3],
        "remind_type": r[4], "target_time": r[5], "attempts": r[6],
        "status": r[7], "sent_at": r[8], "last_error": r[9], "dedup_key": r[10],
        "lease_token": r[11], "lease_until": r[12],
        "scheduled_at": r[13] if len(r) > 13 else "",
        "expires_at": r[14] if len(r) > 14 else "",
        "error_code": r[15] if len(r) > 15 else "",
        "object_snapshot": r[16] if len(r) > 16 else "",
    }


def delivery_dedup_key(group_id: str, obj_type: str, obj_id: int,
                       remind_type: str, target_time: str,
                       offset_hours: float | None = None) -> str:
    key = f"{group_id}|{obj_type}|{obj_id}|{remind_type}|{_conn._canonical_time(target_time)}"
    return f"{key}|{offset_hours:g}" if offset_hours is not None else key


def _canonical_delivery_key(key: str) -> str:
    """兼容旧扫描器的冒号键；身份仍由数据库对象字段确定。

    多节点提醒的键形如 `群|类型|id|类型|时间|节点`，末尾节点后缀
    必须在归一化后原样保留，否则 24h 与 1h 两个节点会被归一成同一个键。
    """
    parts = key.split("|", 4) if "|" in key else key.split(":", 4)
    if len(parts) != 5:
        return key
    try:
        obj_id = int(parts[2])
    except ValueError:
        return key
    target, sep, offset = parts[4].partition("|")
    canonical = delivery_dedup_key(parts[0], parts[1], obj_id, parts[3], target)
    return f"{canonical}{sep}{offset}" if sep else canonical


def _delivery_now(now: datetime | None = None) -> datetime:
    now = now or datetime.now(_conn.TZ)
    return now.replace(tzinfo=_conn.TZ) if now.tzinfo is None else now.astimezone(_conn.TZ)


def _delivery_parse_time(text: str) -> datetime | None:
    if not isinstance(text, str):
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(text.strip(), fmt).replace(tzinfo=_conn.TZ)
        except (ValueError, TypeError):
            continue
    return None


def _legacy_reminder_succeeded_on(conn: sqlite3.Connection, group_id: str, contest_id: int,
                                  remind_type: str, target_time: str) -> bool:
    for row in conn.execute(
        "SELECT target_time FROM reminders WHERE group_id=? AND contest_id=? AND remind_type=?",
        (group_id, contest_id, remind_type),
    ):
        if _conn._canonical_time(row[0]) == target_time:
            return True
    key = delivery_dedup_key(group_id, "legacy_contest", contest_id, remind_type, target_time)
    return conn.execute(
        "SELECT 1 FROM reminder_success_receipts WHERE dedup_key=? UNION ALL "
        "SELECT 1 FROM reminder_deliveries WHERE dedup_key=? AND status='sent' LIMIT 1", (key, key),
    ).fetchone() is not None


def _delivery_invalid_status(conn: sqlite3.Connection, delivery: dict,
                             now: datetime, allowed_groups: set[str] | None = None) -> str | None:
    """发送前按当前对象状态/目标复核；旧计划失效即终结。"""
    if allowed_groups is not None and delivery["group_id"] not in allowed_groups:
        return "cancelled"
    if delivery["obj_type"] == "collection":
        if delivery["remind_type"] != "deadline" or not delivery["group_id"]:
            return "cancelled"
        row = conn.execute(
            "SELECT group_id, status, deadline FROM collections WHERE id = ?", (delivery["obj_id"],)
        ).fetchone()
        if row is None or row[0] != delivery["group_id"] or row[1] != "open" or _conn._canonical_time(row[2]) != delivery["target_time"]:
            return "cancelled"
    elif delivery["obj_type"] == "competition":
        if delivery["remind_type"] == "contest_time":
            return "cancelled"  # 新版只由 contest_start 的30/15分钟计划负责开赛。
        field = {
            "reg_deadline": "official_reg_deadline",
            "contest_start": "official_time",
        }.get(delivery["remind_type"])
        if field is None:
            return "cancelled"
        row = conn.execute(f"SELECT status, {field} FROM competitions WHERE id = ?", (delivery["obj_id"],)).fetchone()
        if row is None or row[0] != "confirmed" or _conn._canonical_time(row[1]) != delivery["target_time"]:
            return "cancelled"
        if delivery["group_id"] not in _contests._competition_target_groups_on(conn, delivery["obj_id"]):
            return "cancelled"
        old_type = {"reg_deadline": "reg_deadline", "contest_start": "contest_time"}[delivery["remind_type"]]
        migrations = conn.execute(
            "SELECT object_id FROM audit_log WHERE action='migrate' AND object_type='contest' AND after_json=?",
            (f"->competition {delivery['obj_id']}",),
        ).fetchall()
        for (old_id,) in migrations:
            if old_id.isdecimal() and _legacy_reminder_succeeded_on(
                    conn, delivery["group_id"], int(old_id), old_type, delivery["target_time"]):
                return "cancelled"
    elif delivery["obj_type"] == "training_session":
        if delivery["remind_type"] != "start_time" or not delivery["group_id"]:
            return "cancelled"
        row = conn.execute(
            f"SELECT {_TRAINING_COLS} FROM training_sessions WHERE id = ?", (delivery["obj_id"],),
        ).fetchone()
        target = _delivery_parse_time(delivery["target_time"])
        if (row is None or row[1] != delivery["group_id"] or target is None
                or not training_occurrence_valid(_row_to_training(row), target)):
            return "cancelled"
    elif delivery["obj_type"] == "legacy_contest":
        field = {"reg_deadline": "reg_deadline", "contest_time": "contest_time"}.get(delivery["remind_type"])
        if field is None or not delivery["group_id"]:
            return "cancelled"
        row = conn.execute(f"SELECT group_id, {field} FROM contests WHERE id=?", (delivery["obj_id"],)).fetchone()
        if (row is None or row[0] != delivery["group_id"]
                or _conn._canonical_time(row[1]) != delivery["target_time"]):
            return "cancelled"
        # 新比赛一旦确认接管，退回草稿待审也不能恢复旧表里的过期内容。
        superseded = conn.execute(
            "SELECT 1 FROM audit_log a JOIN competitions c ON a.after_json = '->competition ' || c.id"
            " WHERE a.action='migrate' AND a.object_type='contest' AND a.object_id=?"
            " AND (c.status='confirmed' OR EXISTS (SELECT 1 FROM competition_versions v"
            " WHERE v.competition_id=c.id AND v.kind='confirmed')) LIMIT 1",
            (str(delivery["obj_id"]),),
        ).fetchone()
        if superseded:
            return "cancelled"
    elif delivery["obj_type"] == "custom_reminder":
        row = conn.execute(
            "SELECT group_id,status,target_time FROM custom_reminders WHERE id=?", (delivery["obj_id"],),
        ).fetchone()
        if (delivery["remind_type"] != "due" or row is None or row[0] != delivery["group_id"]
                or row[1] != "pending" or _conn._canonical_time(row[2]) != delivery["target_time"]):
            return "cancelled"
    else:
        return "cancelled"
    target = _delivery_parse_time(delivery["target_time"])
    if target is None:
        return "cancelled"
    if delivery["obj_type"] == "custom_reminder":
        return None if target <= now else "cancelled"
    if delivery["remind_type"] == "contest_start":
        scheduled = _delivery_parse_time(delivery.get("scheduled_at") or "")
        expires = _delivery_parse_time(delivery.get("expires_at") or "")
        if scheduled is None or expires is None:
            return "cancelled"
        if now < scheduled:
            return None
        return "expired" if now > expires or target <= now else None
    return "expired" if target <= now else None


def claim_delivery(group_id: str, obj_type: str, obj_id: int, remind_type: str,
                   target_time: str, *, now: datetime | None = None,
                   max_attempts: int = 3, lease_seconds: int = 120,
                   allowed_groups: set[str] | None = None,
                   offset_hours: float | None = None) -> str | None:
    """同一写事务复核对象并领取；计数在领取时递增，进程中断也消耗尝试。"""
    if max_attempts < 1 or lease_seconds < 1:
        raise ValueError("提醒尝试次数和发送租约必须大于零")
    now = _delivery_now(now)
    target_time = _conn._canonical_time(target_time)
    key = delivery_dedup_key(group_id, obj_type, obj_id, remind_type, target_time, offset_hours)
    legacy_key = delivery_dedup_key(group_id, obj_type, obj_id, remind_type, target_time)
    if allowed_groups is not None and group_id not in allowed_groups:
        return None
    with _conn._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if obj_type == "competition" and remind_type in ("reg_deadline", "contest_start"):
            old_type = "contest_time" if remind_type == "contest_start" else "reg_deadline"
            inflight = conn.execute(
                "SELECT d.lease_token, d.lease_until FROM reminder_deliveries d"
                " JOIN audit_log a ON a.object_id=CAST(d.obj_id AS TEXT)"
                " WHERE a.action='migrate' AND a.object_type='contest' AND a.after_json=?"
                " AND d.obj_type='legacy_contest' AND d.group_id=? AND d.remind_type=?"
                " AND d.target_time=? AND d.status='sending'",
                (f"->competition {obj_id}", group_id, old_type, target_time),
            ).fetchall()
            for lease_token, lease_until in inflight:
                until = _delivery_parse_time(lease_until)
                if lease_token and until and until > now:
                    return None
        if key != legacy_key:
            legacy = conn.execute(
                "SELECT status FROM reminder_deliveries WHERE dedup_key = ?", (legacy_key,),
            ).fetchone()
            receipt = conn.execute(
                "SELECT sent_at FROM reminder_success_receipts WHERE dedup_key = ?", (legacy_key,),
            ).fetchone()
            if (legacy and legacy[0] == "sent") or receipt:
                if receipt and not legacy:
                    conn.execute(
                        "INSERT OR IGNORE INTO reminder_deliveries"
                        " (group_id, obj_type, obj_id, remind_type, target_time, status, sent_at, dedup_key)"
                        " VALUES (?, ?, ?, ?, ?, 'sent', ?, ?)",
                        (group_id, obj_type, obj_id, remind_type, target_time, receipt[0], legacy_key),
                    )
                return None
        conn.execute(
            "INSERT OR IGNORE INTO reminder_deliveries (group_id, obj_type, obj_id, remind_type, target_time, dedup_key)"
            " VALUES (?, ?, ?, ?, ?, ?)", (group_id, obj_type, obj_id, remind_type, target_time, key),
        )
        row = conn.execute(f"SELECT {_DELIVERY_COLS} FROM reminder_deliveries WHERE dedup_key = ?", (key,)).fetchone()
        delivery = _row_to_delivery(row)
        if obj_type == "legacy_contest":
            history = conn.execute(
                "SELECT target_time, sent_at FROM reminders WHERE group_id=? AND contest_id=? AND remind_type=?",
                (group_id, obj_id, remind_type),
            ).fetchall()
            for history_target, sent_at in history:
                if _conn._canonical_time(history_target) == target_time:
                    conn.execute(
                        "INSERT OR IGNORE INTO reminder_success_receipts (dedup_key, lease_token, sent_at) VALUES (?, ?, ?)",
                        (key, "legacy-history", sent_at),
                    )
                    return None
        if remind_type == "contest_start":
            scheduled = _delivery_parse_time(delivery.get("scheduled_at") or "")
            if scheduled is None or now < scheduled or delivery["status"] == "cancelled":
                return None
        if delivery["status"] == "sent" or conn.execute(
            "SELECT 1 FROM reminder_success_receipts WHERE dedup_key = ?", (key,)
        ).fetchone():
            return None
        if delivery["status"] == "sending":
            until = _delivery_parse_time(delivery["lease_until"])
            if until is not None and until > now and delivery["lease_token"]:
                return None
        status = _delivery_invalid_status(conn, delivery, now, allowed_groups)
        if status:
            if delivery["status"] != "sent":
                conn.execute(
                    "UPDATE reminder_deliveries SET status = ?, lease_token = '', lease_until = '' WHERE id = ?",
                    (status, delivery["id"]),
                )
            return None
        if delivery["status"] in ("failed", "expired"):
            return None
        if delivery["attempts"] >= max_attempts:
            conn.execute(
                "UPDATE reminder_deliveries SET status = 'failed', lease_token = '', lease_until = '' WHERE id = ?",
                (delivery["id"],),
            )
            return None
        token = uuid.uuid4().hex
        until = (now + timedelta(seconds=lease_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        snapshot = ""
        if obj_type == "custom_reminder":
            row = conn.execute(f"SELECT {_CUSTOM_REMINDER_COLS} FROM custom_reminders WHERE id=?", (obj_id,)).fetchone()
            snapshot = json.dumps(_row_to_custom_reminder(row), ensure_ascii=False, sort_keys=True)
        conn.execute(
            "UPDATE reminder_deliveries SET status = 'sending', attempts = attempts + 1, lease_token = ?, lease_until = ?,"
            " object_snapshot=? WHERE id = ?", (token, until, snapshot, delivery["id"]),
        )
        return token


def validate_delivery_claim(dedup_key: str, token: str, *, now: datetime | None = None,
                            allowed_groups: set[str] | None = None) -> bool:
    """投递前再检查租约和对象，已关闭/改期的计划不发送。"""
    now = _delivery_now(now)
    with _conn._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"SELECT {_DELIVERY_COLS} FROM reminder_deliveries WHERE dedup_key = ? AND status = 'sending' AND lease_token = ?",
            (_canonical_delivery_key(dedup_key), token),
        ).fetchone()
        if row is None:
            return False
        if conn.execute("SELECT 1 FROM reminder_success_receipts WHERE dedup_key = ?",
                        (_canonical_delivery_key(dedup_key),)).fetchone():
            return False
        delivery = _row_to_delivery(row)
        status = _delivery_invalid_status(conn, delivery, now, allowed_groups)
        if status:
            conn.execute(
                "UPDATE reminder_deliveries SET status = ?, lease_token = '', lease_until = '' WHERE id = ?",
                (status, delivery["id"]),
            )
            return False
        until = _delivery_parse_time(delivery["lease_until"])
        return until is not None and until > now


def record_delivery_success(dedup_key: str, token: str, *, now: datetime | None = None) -> bool:
    """独立提交当前发送者的成功事实；同一回执幂等，旧租约不能冒认成功。"""
    key = _canonical_delivery_key(dedup_key)
    now_text = _delivery_now(now).strftime("%Y-%m-%d %H:%M:%S")
    with _conn._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR IGNORE INTO reminder_success_receipts (dedup_key, lease_token, sent_at)"
            " SELECT dedup_key, lease_token, ? FROM reminder_deliveries"
            " WHERE dedup_key = ? AND status = 'sending' AND lease_token = ?",
            (now_text, key, token),
        )
        row = conn.execute("SELECT lease_token FROM reminder_success_receipts WHERE dedup_key = ?", (key,)).fetchone()
        return row is not None and row[0] == token


def finish_delivery(dedup_key: str, token: str, ok: bool, *, error: str = "",
                    error_code: str = "", now: datetime | None = None, max_attempts: int = 3) -> bool:
    """成功事实先独立提交；状态更新异常不撤销成功，也不会导致租约过期重发。"""
    key = _canonical_delivery_key(dedup_key)
    now_text = _delivery_now(now).strftime("%Y-%m-%d %H:%M:%S")
    if ok and not record_delivery_success(key, token, now=now):
        return False
    try:
        with _conn._connect() as conn:
            cur = conn.execute(
                "UPDATE reminder_deliveries SET status = CASE WHEN ? THEN 'sent' WHEN attempts >= ? THEN 'failed' ELSE 'pending' END,"
                " sent_at = CASE WHEN ? THEN ? ELSE sent_at END, last_error = ?, error_code = ?,"
                " lease_token = '', lease_until = ''"
                " WHERE dedup_key = ? AND status = 'sending' AND lease_token = ?",
                (ok, max_attempts, ok, now_text, "" if ok else (error or "发送失败"),
                 "" if ok else (error_code or "send_failed"), key, token),
            )
            return ok or cur.rowcount > 0
    except sqlite3.Error:
        if not ok:
            raise
        logger.exception("提醒状态更新失败，已保留成功回执: %s", key)
        return True


def ensure_delivery(group_id: str, obj_type: str, obj_id: int,
                    remind_type: str, target_time: str,
                    offset_hours: float | None = None) -> bool:
    """登记一条待发送提醒；offset为空时沿用旧去重身份。"""
    target_time = _conn._canonical_time(target_time)
    key = delivery_dedup_key(group_id, obj_type, obj_id, remind_type, target_time, offset_hours)
    with _conn._connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO reminder_deliveries (group_id, obj_type, obj_id,"
            " remind_type, target_time, status, dedup_key)"
            " VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (group_id, obj_type, obj_id, remind_type, target_time, key),
        )
        return cur.rowcount > 0


def get_delivery_by_key(dedup_key: str) -> dict | None:
    with _conn._connect() as conn:
        row = conn.execute(
            f"SELECT {_DELIVERY_COLS} FROM reminder_delivery_states WHERE dedup_key = ?",
            (_canonical_delivery_key(dedup_key),),
        ).fetchone()
    return _row_to_delivery(row) if row else None


def get_target_names(pairs: Iterable[tuple[str, int]]) -> dict[tuple[str, int], str]:
    """批量解析提醒目标名称，键为 (obj_type, obj_id)；已删除或不支持的类型不返回。"""
    grouped: dict[str, set[int]] = {}
    for obj_type, obj_id in pairs:
        if obj_type not in _contests._TARGET_NAME_COLUMNS:
            continue
        try:
            grouped.setdefault(obj_type, set()).add(int(obj_id))
        except (TypeError, ValueError):
            continue
    names: dict[tuple[str, int], str] = {}
    if not grouped:
        return names
    with _conn._connect() as conn:
        for obj_type, ids in grouped.items():
            table, column = _contests._TARGET_NAME_COLUMNS[obj_type]
            marks = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT id, {column} FROM {table} WHERE id IN ({marks})", sorted(ids),
            ).fetchall()
            for row in rows:
                text = (row[1] or "").strip()
                if text:
                    names[(obj_type, int(row[0]))] = text
    return names


COURSE_REMIND_SETTING = "course_remind"


_COURSE_REMIND_ON = "on"


_COURSE_REMIND_OFF = "off"


_CUSTOM_REMINDER_COLS = (
    "id, group_id, content, target_time, repeat_rule, weekday, time_of_day,"
    " status, created_by, created_at, updated_at"
)


_CUSTOM_REMINDER_STATUSES = ("pending", "done", "cancelled")


def _row_to_custom_reminder(row: sqlite3.Row) -> dict:
    return {
        "id": row[0], "group_id": row[1], "content": row[2], "target_time": row[3],
        "repeat_rule": row[4], "weekday": row[5], "time_of_day": row[6],
        "status": row[7], "created_by": row[8], "created_at": row[9],
        "updated_at": row[10],
    }


def create_custom_reminder(group_id: str, content: str, target_time: str, *,
                           repeat_rule: str = "none", weekday: str = "",
                           time_of_day: str = "", operator: str = "") -> int:
    """新建自定义提醒，返回新 id。"""
    group_id = _conn._as_str(group_id)
    content = _conn._as_str(content).strip()
    if not content:
        raise ValueError("提醒内容不能为空")
    target = _conn._canonical_time(target_time)
    if not target:
        raise ValueError("提醒时间不能为空")
    rule = "weekly" if repeat_rule == "weekly" else "none"
    with _conn._connect() as conn:
        cur = conn.execute(
            "INSERT INTO custom_reminders (group_id, content, target_time, repeat_rule,"
            " weekday, time_of_day, status, created_by, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,'pending',?,?,?)",
            (group_id, content, target, rule, _conn._as_str(weekday), _conn._as_str(time_of_day),
             _conn._as_str(operator), _conn.now_str(), _conn.now_str()),
        )
        new_id = cur.lastrowid
        _admin.add_audit(operator, group_id, "create", "custom_reminder", str(new_id),
                  "", content, conn=conn)
    return new_id


def list_custom_reminders(group_id: str, *, statuses: tuple[str, ...] = ("pending",)) -> list[dict]:
    """按触发时间升序返回某群自定义提醒。"""
    where = "group_id = ?"
    args: list = [_conn._as_str(group_id)]
    if statuses:
        where += " AND status IN (" + ",".join("?" for _ in statuses) + ")"
        args.extend(statuses)
    with _conn._connect() as conn:
        rows = conn.execute(
            f"SELECT {_CUSTOM_REMINDER_COLS} FROM custom_reminders WHERE {where}"
            " ORDER BY target_time, id", args,
        ).fetchall()
    return [_row_to_custom_reminder(r) for r in rows]


def get_custom_reminder(reminder_id: int) -> dict | None:
    try:
        rid = int(reminder_id)
    except (TypeError, ValueError):
        return None
    with _conn._connect() as conn:
        row = conn.execute(
            f"SELECT {_CUSTOM_REMINDER_COLS} FROM custom_reminders WHERE id = ?", (rid,)
        ).fetchone()
    return _row_to_custom_reminder(row) if row else None


def complete_custom_reminder(reminder: dict, dedup_key: str, token: str, *, now=None) -> bool:
    """回执、提醒状态投影同事务；清理会保留 sending 直到此处提交完成。"""
    key = _canonical_delivery_key(dedup_key)
    with _conn._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM reminder_success_receipts WHERE dedup_key=? AND lease_token=?",
                            (key, token)).fetchone():
            return False
        row = conn.execute(f"SELECT {_CUSTOM_REMINDER_COLS} FROM custom_reminders WHERE id=?",
                           (reminder["id"],)).fetchone()
        # 相同时间的本次发送已经完成；保留最新内容，改期后的新实例不推进。
        current = _row_to_custom_reminder(row) if row is not None else None
        if (current is not None and current["group_id"] == reminder["group_id"]
                and current["target_time"] == reminder["target_time"] and current["status"] == "pending"):
            target = _delivery_parse_time(current["target_time"])
            if current["repeat_rule"] == "weekly" and target is not None:
                next_time = (target + timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
                conn.execute("UPDATE custom_reminders SET target_time=?,updated_at=? WHERE id=?",
                             (next_time, _delivery_now(now).strftime("%Y-%m-%d %H:%M:%S"), reminder["id"]))
            else:
                conn.execute("UPDATE custom_reminders SET status='done',updated_at=? WHERE id=?",
                             (_delivery_now(now).strftime("%Y-%m-%d %H:%M:%S"), reminder["id"]))
        conn.execute("UPDATE reminder_deliveries SET status='sent',lease_token='',lease_until='',last_error='',"
                     "error_code='',sent_at=? WHERE dedup_key=? AND lease_token=?",
                     (_delivery_now(now).strftime("%Y-%m-%d %H:%M:%S"), key, token))
        return True


def recover_custom_reminder_delivery(reminder: dict, *, now=None) -> bool:
    """重启/状态投影失败后用成功回执与原发送快照推进，绝不重发。"""
    key = delivery_dedup_key(reminder["group_id"], "custom_reminder", reminder["id"], "due", reminder["target_time"])
    with _conn._connect() as conn:
        row = conn.execute("SELECT d.object_snapshot,r.lease_token FROM reminder_deliveries d"
                           " JOIN reminder_success_receipts r ON r.dedup_key=d.dedup_key"
                           " WHERE d.dedup_key=?", (key,)).fetchone()
    if row is None or not row[0]:
        return False
    original = json.loads(row[0])
    complete_custom_reminder(original, key, row[1], now=now)
    return True


def update_custom_reminder(reminder_id: int, *, content: str | None = None,
                           target_time: str | None = None, status: str | None = None,
                           operator: str = "") -> bool:
    """修改自定义提醒的时间/内容/状态；返回是否有行被更新。"""
    try:
        rid = int(reminder_id)
    except (TypeError, ValueError):
        return False
    changes: dict[str, object] = {}
    if content is not None:
        text = _conn._as_str(content).strip()
        if not text:
            raise ValueError("提醒内容不能为空")
        changes["content"] = text
    if target_time is not None:
        target = _conn._canonical_time(target_time)
        if not target:
            raise ValueError("提醒时间不能为空")
        changes["target_time"] = target
    if status is not None:
        if status not in _CUSTOM_REMINDER_STATUSES:
            raise ValueError("状态必须是 pending/done/cancelled")
        changes["status"] = status
    if not changes:
        return False
    assignments = ", ".join(f"{field} = ?" for field in changes)
    with _conn._connect() as conn:
        cur = conn.execute(
            f"UPDATE custom_reminders SET {assignments}, updated_at = ? WHERE id = ?",
            [*changes.values(), _conn.now_str(), rid],
        )
        if cur.rowcount:
            _admin.add_audit(operator, "", "update", "custom_reminder", str(rid), "",
                      ",".join(changes), conn=conn)
        return cur.rowcount > 0


def set_custom_reminder_repeat(reminder_id: int, *, repeat_rule: str = "none",
                               weekday: str = "", time_of_day: str = "",
                               operator: str = "") -> bool:
    """单独更新循环规则（/提醒修改 改周期时用），与内容/时间更新解耦。"""
    try:
        rid = int(reminder_id)
    except (TypeError, ValueError):
        return False
    rule = "weekly" if repeat_rule == "weekly" else "none"
    with _conn._connect() as conn:
        cur = conn.execute(
            "UPDATE custom_reminders SET repeat_rule = ?, weekday = ?, time_of_day = ?,"
            " updated_at = ? WHERE id = ?",
            (rule, _conn._as_str(weekday), _conn._as_str(time_of_day), _conn.now_str(), rid),
        )
        if cur.rowcount:
            _admin.add_audit(operator, "", "update", "custom_reminder", str(rid), "",
                      "repeat_rule=" + rule, conn=conn)
        return cur.rowcount > 0


def delete_custom_reminder(reminder_id: int, operator: str = "") -> bool:
    """删除自定义提醒（物理删除，仅本表行，不触碰任何用户文件）。"""
    try:
        rid = int(reminder_id)
    except (TypeError, ValueError):
        return False
    with _conn._connect() as conn:
        cur = conn.execute("DELETE FROM custom_reminders WHERE id = ?", (rid,))
        if cur.rowcount:
            _admin.add_audit(operator, "", "delete", "custom_reminder", str(rid), "", "", conn=conn)
        return cur.rowcount > 0


def list_due_custom_reminders(now, limit: int = 200) -> list[dict]:
    """返回所有到期待发自定义提醒（target_time <= now）。"""
    stamp = _conn._canonical_time(now.strftime("%Y-%m-%d %H:%M:%S") if hasattr(now, "strftime") else str(now))
    with _conn._connect() as conn:
        rows = conn.execute(
            f"SELECT {_CUSTOM_REMINDER_COLS} FROM custom_reminders"
            " WHERE status = 'pending' AND target_time <= ? ORDER BY target_time, id LIMIT ?",
            (stamp, limit),
        ).fetchall()
    return [_row_to_custom_reminder(r) for r in rows]


def delivery_status_counts(group_id: str) -> dict[str, int]:
    with _conn._connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM reminder_delivery_states WHERE group_id = ? GROUP BY status", (group_id,)
        ).fetchall()
    return dict(rows)
