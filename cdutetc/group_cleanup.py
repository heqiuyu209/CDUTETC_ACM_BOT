"""在线按群清理：共享比赛归档，显式测试记录精确删除，事务前完整备份。"""

import hashlib
import json
import logging
import secrets
import threading
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from cdutetc import config, database as db
from scripts.reset_business_data import _backup

logger = logging.getLogger(__name__)
_MODES = {"expired_contests": 30, "test_data": 1}
_TABLES = {
    "competition": "competitions", "legacy_contest": "contests",
    "collection": "collections", "training_session": "training_sessions",
    "custom_reminder": "custom_reminders", "question": "unanswered_questions",
    "faq": "group_faq", "publication": "pending_publications",
}
_PREVIEWS = {}
_LOCK = threading.RLock()
_MAX_ITEMS = 2000


def _now(value=None):
    return db._delivery_now(value)


def _group(group_id):
    if not isinstance(group_id, str) or not group_id.strip() or group_id.strip() not in config.ALLOWED_GROUPS:
        raise ValueError("请选择一个已授权群")
    return group_id.strip()


def _parameters(mode, days, *, rule=False):
    if mode not in _MODES:
        raise ValueError("清理类型须为过期比赛或测试数据")
    minimum = 1 if rule or mode == "expired_contests" else 0
    if type(days) is not int or not minimum <= days <= 36500:
        raise ValueError(f"保留天数须为 {minimum} 至 36500 的整数")


def _identity(object_type, obj_id):
    if object_type not in _TABLES or type(obj_id) is not int or obj_id <= 0:
        raise ValueError("对象类型或编号无效")


def _path(conn):
    filename = next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main")
    if not filename:
        raise ValueError("在线清理需要可备份的 SQLite 文件")
    return Path(filename).resolve(strict=True)


def _rows(conn, sql, args=()):
    return conn.execute(sql, args).fetchall()


def _object(conn, gid, kind, oid):
    cursor = conn.execute(f"SELECT * FROM {_TABLES[kind]} WHERE id=?", (oid,))
    row = cursor.fetchone()
    if row is None:
        return None
    item = dict(zip((col[0] for col in cursor.description), row))
    if kind == "competition":
        if gid not in db._competition_target_groups_on(conn, oid):
            return None
    elif item["group_id"] != gid:
        return None
    return item


def _sending(conn, gid, kind, oid):
    if conn.execute("SELECT 1 FROM reminder_deliveries WHERE group_id=? AND obj_type=?"
                    " AND obj_id=? AND status='sending' LIMIT 1", (gid, kind, oid)).fetchone():
        return True
    # 迁移后的共享比赛也可能仍有原旧链路的发送者，保留其回执资格。
    return kind == "competition" and conn.execute(
        "SELECT 1 FROM reminder_deliveries d JOIN audit_log a ON a.object_id=CAST(d.obj_id AS TEXT)"
        " WHERE a.action='migrate' AND a.object_type='contest' AND a.after_json=?"
        " AND d.group_id=? AND d.obj_type='legacy_contest' AND d.status='sending' LIMIT 1",
        (f"->competition {oid}", gid),
    ).fetchone() is not None


def _snapshot(conn, gid, kind, item):
    oid = item["id"]
    state = {"object": item, "mark": _rows(conn, "SELECT * FROM group_test_records"
        " WHERE group_id=? AND object_type=? AND object_id=?", (gid, kind, oid)),
        "deliveries": _rows(conn, "SELECT * FROM reminder_deliveries WHERE group_id=?"
        " AND obj_type=? AND obj_id=? ORDER BY id", (gid, kind, oid))}
    if kind == "competition":
        for table in ("competition_notify_groups", "weekly_contest_runs", "competition_versions",
                      "competition_sources", "competition_pending_updates"):
            state[table] = _rows(conn, f"SELECT * FROM {table} WHERE competition_id=? ORDER BY rowid", (oid,))
        state["archive"] = _rows(conn, "SELECT * FROM group_competition_archives"
                                 " WHERE group_id=? AND competition_id=?", (gid, oid))
        state["location"] = _rows(conn, "SELECT * FROM group_competition_locations"
                                  " WHERE group_id=? AND competition_id=?", (gid, oid))
    elif kind == "collection":
        state["responses"] = _rows(conn, "SELECT * FROM collection_responses WHERE collection_id=? ORDER BY id", (oid,))
    elif kind == "faq":
        state["questions"] = _rows(conn, "SELECT * FROM unanswered_questions WHERE faq_id=? ORDER BY id", (oid,))
    return hashlib.sha256(json.dumps(state, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _expired(item, kind, current, days):
    fields = (("official_time", "official_reg_start", "official_reg_deadline")
              if kind == "competition" else ("contest_time", "reg_deadline"))
    if not item.get(fields[0]):
        return False
    dates = []
    for field in fields:
        if item.get(field):
            parsed = db._delivery_parse_time(item[field])
            if parsed is None:
                return False
            dates.append(parsed)
    return bool(dates) and max(dates) < current - timedelta(days=days)


def _eligible(conn, gid, mode, days, kind, item, current):
    if _sending(conn, gid, kind, item["id"]):
        return False
    if mode == "expired_contests":
        return kind in ("competition", "legacy_contest") and _expired(item, kind, current, days)
    mark = conn.execute("SELECT marked_at FROM group_test_records WHERE group_id=?"
                        " AND object_type=? AND object_id=?", (gid, kind, item["id"])).fetchone()
    if mark is None:
        return False
    if days:
        marked = db._delivery_parse_time(mark[0])
        if marked is None or marked > current - timedelta(days=days):
            return False
    if kind == "publication" and item["status"] != "pending":
        return False
    if kind == "collection" and conn.execute(
        "SELECT 1 FROM collection_responses WHERE collection_id=? AND group_id<>? LIMIT 1",
        (item["id"], gid),
    ).fetchone():
        return False
    if kind == "faq" and conn.execute(
        "SELECT 1 FROM unanswered_questions WHERE faq_id=? AND group_id<>? LIMIT 1",
        (item["id"], gid),
    ).fetchone():
        return False
    return True


def _plan(conn, gid, mode, days, current):
    if mode == "expired_contests":
        identities = [("competition", row[0]) for row in conn.execute("SELECT id FROM competitions ORDER BY id")]
        identities += [("legacy_contest", row[0]) for row in conn.execute(
            "SELECT id FROM contests WHERE group_id=? ORDER BY id", (gid,))]
    else:
        identities = _rows(conn, "SELECT object_type, object_id FROM group_test_records"
                           " WHERE group_id=? ORDER BY object_type,object_id", (gid,))
    items, skipped = [], 0
    for kind, oid in identities:
        if kind not in _TABLES:
            skipped += 1
            continue
        item = _object(conn, gid, kind, oid)
        if item is None:
            continue
        if not _eligible(conn, gid, mode, days, kind, item, current) or len(items) >= _MAX_ITEMS:
            skipped += 1
            continue
        items.append({"type": kind, "id": oid,
                      "title": item.get("name") or item.get("title") or item.get("question") or item.get("content") or "",
                      "snapshot": _snapshot(conn, gid, kind, item)})
    return items, skipped


def preview_cleanup(group_id, mode, days=30, operator="", now=None):
    gid, current = _group(group_id), _now(now)
    _parameters(mode, days)
    with closing(db._connect()) as conn:
        conn.execute("BEGIN")
        path = _path(conn)
        items, skipped = _plan(conn, gid, mode, days, current)
    token = secrets.token_hex(6)
    with _LOCK:
        # 不写业务或配置表；重启后必须重新预览，不能复用旧进程中的确认码。
        for key, value in list(_PREVIEWS.items()):
            if value["expires"] < current:
                _PREVIEWS.pop(key)
        if len(_PREVIEWS) >= 128:
            _PREVIEWS.pop(next(iter(_PREVIEWS)))
        _PREVIEWS[token] = {"group": gid, "operator": operator, "path": path, "mode": mode,
                            "days": days, "items": items, "skipped": skipped,
                            "expires": current + timedelta(minutes=10)}
    return {"token": token, "items": [{k: v for k, v in item.items() if k != "snapshot"} for item in items],
            "skipped": skipped}


def _clear(conn, gid, kind, oid, operator, current, mode):
    if kind == "competition":
        conn.execute("INSERT INTO group_competition_archives"
                     "(group_id,competition_id,reason,archived_at,archived_by) VALUES (?,?,?,?,?)",
                     (gid, oid, mode, current.strftime("%Y-%m-%d %H:%M:%S"), operator))
    else:
        if kind == "collection":
            conn.execute("DELETE FROM collection_responses WHERE collection_id=? AND group_id=?", (oid, gid))
        elif kind == "faq":
            # 删除测试答案后重新开放所关联的本群问题，保留问题原文及历史编号。
            conn.execute("UPDATE unanswered_questions SET faq_id=0, status=CASE WHEN status='answered'"
                         " THEN 'open' ELSE status END WHERE faq_id=? AND group_id=?", (oid, gid))
        conn.execute(f"DELETE FROM {_TABLES[kind]} WHERE id=? AND group_id=?", (oid, gid))
    conn.execute("UPDATE reminder_deliveries SET status='cancelled',lease_token='',lease_until=''"
                 " WHERE group_id=? AND obj_type=? AND obj_id=? AND status='pending'", (gid, kind, oid))
    conn.execute("DELETE FROM group_test_records WHERE group_id=? AND object_type=? AND object_id=?", (gid, kind, oid))
    db.add_audit(operator, gid, "group_cleanup", kind, str(oid), "", mode, conn=conn)


def _execute(conn, gid, plan, current, operator):
    valid, skipped = [], plan["skipped"]
    for expected in plan["items"]:
        kind, oid = expected["type"], expected["id"]
        item = _object(conn, gid, kind, oid)
        if (item is None or not _eligible(conn, gid, plan["mode"], plan["days"], kind, item, current)
                or _snapshot(conn, gid, kind, item) != expected["snapshot"]):
            skipped += 1
        else:
            valid.append(expected)
    backup = None
    if valid:
        path = _path(conn)
        backup = _backup(path, path.parent / "backups")
    cleared = {}
    for item in valid:
        _clear(conn, gid, item["type"], item["id"], operator, current, plan["mode"])
        cleared[item["type"]] = cleared.get(item["type"], 0) + 1
    return {"cleared": cleared, "skipped": skipped, "backup": backup}


def apply_cleanup(group_id, token, operator="", now=None):
    gid, current = _group(group_id), _now(now)
    with _LOCK:
        plan = _PREVIEWS.get(token)
        if (not plan or plan["group"] != gid or plan["operator"] != operator or current >= plan["expires"]):
            raise ValueError("预览码无效、已过期，或不属于当前操作者与群，请重新预览")
        with closing(db._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if _path(conn) != plan["path"]:
                raise ValueError("数据库已变化，请重新预览")
            result = _execute(conn, gid, plan, current, operator)
        _PREVIEWS.pop(token)
    return result


def mark_test(group_id, object_type, obj_id, operator="", now=None):
    gid = _group(group_id)
    _identity(object_type, obj_id)
    with closing(db._connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if _object(conn, gid, object_type, obj_id) is None:
            raise ValueError("对象不存在或不属于当前群")
        conn.execute("INSERT INTO group_test_records(group_id,object_type,object_id,marked_at,marked_by)"
                     " VALUES (?,?,?,?,?) ON CONFLICT(group_id,object_type,object_id) DO UPDATE"
                     " SET marked_at=excluded.marked_at,marked_by=excluded.marked_by",
                     (gid, object_type, obj_id, _now(now).strftime("%Y-%m-%d %H:%M:%S"), operator))
        db.add_audit(operator, gid, "mark_test", object_type, str(obj_id), conn=conn)


def unmark_test(group_id, object_type, obj_id, operator="", now=None):
    gid = _group(group_id)
    _identity(object_type, obj_id)
    with closing(db._connect()) as conn, conn:
        conn.execute("DELETE FROM group_test_records WHERE group_id=? AND object_type=? AND object_id=?",
                     (gid, object_type, obj_id))
        db.add_audit(operator, gid, "unmark_test", object_type, str(obj_id), conn=conn)


def restore_competition(group_id, competition_id, operator=""):
    gid = _group(group_id)
    _identity("competition", competition_id)
    with closing(db._connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM competitions WHERE id=?", (competition_id,)).fetchone():
            return False
        changed = conn.execute("DELETE FROM group_competition_archives WHERE group_id=? AND competition_id=?",
                               (gid, competition_id)).rowcount > 0
        if changed:
            current = _now()
            # 本群归档取消过的未来节点可恢复未耗尽尝试次数，成功/失败事实不重置。
            conn.execute("UPDATE reminder_deliveries SET status='pending' WHERE group_id=?"
                         " AND obj_type='competition' AND obj_id=? AND remind_type='contest_start'"
                         " AND status='cancelled' AND attempts<3 AND scheduled_at>?"
                         " AND target_time=(SELECT official_time FROM competitions WHERE id=? AND status='confirmed')"
                         " AND NOT EXISTS (SELECT 1 FROM reminder_success_receipts r"
                         " WHERE r.dedup_key=reminder_deliveries.dedup_key)",
                         (gid, competition_id, current.strftime("%Y-%m-%d %H:%M:%S"), competition_id))
            # 同步函数默认会扫描所有群，因此显式限制为本群。
            db._sync_contest_start_reminders(conn, competition_id, {gid}, now=current, only_group=gid)
            db.add_audit(operator, gid, "restore_archive", "competition", str(competition_id), conn=conn)
        return changed


def archive_competition(group_id, competition_id, operator="", reason="cancelled"):
    """老师取消本群比赛时沿用归档语义，不改共享比赛或通知范围。"""
    gid = _group(group_id)
    _identity("competition", competition_id)
    with closing(db._connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        item = _object(conn, gid, "competition", competition_id)
        if item is None:
            return False
        if _sending(conn, gid, "competition", competition_id):
            raise ValueError("本群比赛提醒正在发送，请稍后再取消")
        path = _path(conn)
        _backup(path, path.parent / "backups")
        _clear(conn, gid, "competition", competition_id, operator, _now(), reason)
        return True


def is_competition_archived(group_id, competition_id):
    gid = _group(group_id)
    _identity("competition", competition_id)
    # 数据门面允许调用方注入共享连接；沿用其事务上下文，不关闭借来的连接。
    with db._connect() as conn:
        return conn.execute("SELECT 1 FROM group_competition_archives WHERE group_id=? AND competition_id=?",
                            (gid, competition_id)).fetchone() is not None


def _rule_key(mode):
    return "group_cleanup:" + mode


def _rules_on(conn, gid):
    result = {mode: {"days": days, "enabled": False, "last_run": ""} for mode, days in _MODES.items()}
    for mode in result:
        row = conn.execute("SELECT value FROM group_settings WHERE group_id=? AND key=?",
                           (gid, _rule_key(mode))).fetchone()
        if row:
            try:
                rule = json.loads(row[0])
                _parameters(mode, rule["days"], rule=True)
                if type(rule["enabled"]) is not bool:
                    raise ValueError("enabled must be bool")
                result[mode] = {"days": rule["days"], "enabled": rule["enabled"],
                                "last_run": str(rule.get("last_run") or "")}
            except (ValueError, TypeError, KeyError, AttributeError):
                logger.warning("忽略无效群清理规则: %s %s", gid, mode)
    return result


def _write_rule(conn, gid, mode, rule, operator):
    value = json.dumps(rule, ensure_ascii=False, sort_keys=True)
    conn.execute("INSERT INTO group_settings(group_id,key,value,updated_at) VALUES (?,?,?,?)"
                 " ON CONFLICT(group_id,key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                 (gid, _rule_key(mode), value, db.now_str()))
    db.add_audit(operator, gid, "cleanup_rule", "group_setting", _rule_key(mode), "", value, conn=conn)


def set_rule(group_id, mode, days, enabled, operator=""):
    gid = _group(group_id)
    _parameters(mode, days, rule=True)
    if type(enabled) is not bool:
        raise ValueError("请明确开启或关闭")
    with closing(db._connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        old = _rules_on(conn, gid)[mode]
        _write_rule(conn, gid, mode, {"days": days, "enabled": enabled, "last_run": old["last_run"]}, operator)


def get_rules(group_id):
    gid = _group(group_id)
    with closing(db._connect()) as conn:
        return _rules_on(conn, gid)


def run_rules(now=None):
    current, results = _now(now), {}
    today = current.date().isoformat()
    for gid in sorted(set(config.ALLOWED_GROUPS)):
        for mode in _MODES:
            try:
                with closing(db._connect()) as conn, conn:
                    conn.execute("BEGIN IMMEDIATE")
                    rule = _rules_on(conn, gid)[mode]
                    if not rule["enabled"] or rule["last_run"] == today:
                        continue
                    items, skipped = _plan(conn, gid, mode, rule["days"], current)
                    result = _execute(conn, gid, {"items": items, "skipped": skipped,
                                      "mode": mode, "days": rule["days"]}, current, "cleanup:auto")
                    _write_rule(conn, gid, mode, {**rule, "last_run": today}, "cleanup:auto")
                combined = results.setdefault(gid, {"cleared": {}, "skipped": 0, "backups": []})
                for kind, count in result["cleared"].items():
                    combined["cleared"][kind] = combined["cleared"].get(kind, 0) + count
                combined["skipped"] += result["skipped"]
                if result["backup"]:
                    combined["backups"].append(result["backup"])
            except Exception as exc:
                logger.exception("群清理规则失败: %s %s", gid, mode)
                results.setdefault(gid, {"cleared": {}, "skipped": 0, "backups": []}).setdefault("errors", []).append(str(exc))
    return results
