"""比赛主数据访问：contests 兼容表、competitions 及版本快照、比赛编辑与赛制迁移。"""

import logging
import hashlib
import json
import sqlite3
from datetime import datetime
from urllib.parse import urlsplit
from cdutetc import config

from cdutetc.db import admin as _admin
from cdutetc.db import connection as _conn
from cdutetc.db import sources as _sources
from cdutetc.db import training as _training

logger = logging.getLogger(__name__)


logger = logging.getLogger(__name__)


# LEGACY(P1): contests/reminders 旧表读写，仅供历史数据迁移与清理；新代码请用 competitions 新链。
def save_contest(group_id: str, info: dict) -> int:
    """写入一条群级比赛记录，返回新记录 id。字段必须是字符串，否则抛 ValueError。"""
    values = (
        group_id.strip(),
        _conn._as_str(info.get("name")),
        _conn._canonical_time(_conn._as_str(info.get("reg_deadline") or "")),
        _conn._canonical_time(_conn._as_str(info.get("contest_time") or "")),
        _conn._as_str(info.get("mode") or ""),
        _conn._as_str(info.get("location") or ""),
        _conn._as_str(info.get("source_link") or ""),
        _conn._as_str(info.get("raw_note") or ""),
        _conn.now_str(),
    )
    with _conn._connect() as conn:
        cur = conn.execute(
            "INSERT INTO contests (group_id, name, reg_deadline, contest_time, mode, location, source_link, raw_note, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        return cur.lastrowid


_CONTEST_COLS = "id, group_id, name, reg_deadline, contest_time, mode, location, source_link, raw_note"


def _row_to_contest(r: sqlite3.Row) -> dict:
    return {
        "id": r[0], "group_id": r[1], "name": r[2], "reg_deadline": r[3],
        "contest_time": r[4], "mode": r[5], "location": r[6],
        "source_link": r[7], "raw_note": r[8],
    }


# LEGACY(P1): contests/reminders 旧表读写，仅供历史数据迁移与清理；新代码请用 competitions 新链。
def list_contests(group_id: str | None = None) -> list[dict]:
    """查询群级比赛记录。

    - 传入 group_id：只返回该群记录（旧记录 group_id 为空，不会混入任何具体群）。
    - 不传：返回全部记录（含未归属旧记录），仅用于管理/迁移场景。
    """
    with _conn._connect() as conn:
        if group_id is None:
            rows = conn.execute(
                f"SELECT {_CONTEST_COLS} FROM contests ORDER BY id DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_CONTEST_COLS} FROM contests WHERE group_id = ? ORDER BY id DESC",
                (group_id,),
            ).fetchall()
    return [_row_to_contest(r) for r in rows]


def list_question_contests(group_id: str, limit: int = 5, *, order: str = "recent") -> list[dict]:
    """查询问答用比赛：已确认公共比赛 + 本群已归属的旧记录。

    调用方须先校验群白名单。v2 公共正式比赛供允许群共享，草稿与待审
    更新不进入上下文；旧记录在 SQL 层按群过滤。按 ``order`` 排序并限量，
    统一为问答使用的名称、时间、形式、地点和来源字段，并带出 id 供调用方
    做同时间排序的稳定兜底。

    ``order="recent"``：按最近更新/创建时间倒序（问答上下文用）；
    ``order="start"``：按开赛时间正序 —— 有明确开赛时间的在前且由近到远，
    时间未定（空值）排最后，开赛时间相同按 id 升序（「近期比赛」展示用）。
    """
    if not group_id or limit <= 0:
        return []
    fields = ("name", "reg_deadline", "contest_time", "mode", "location", "source_link", "id")
    order_sql = (
        "CASE WHEN contest_time IS NULL OR TRIM(contest_time) = '' THEN 1 ELSE 0 END,"
        " contest_time ASC, id ASC"
        if order == "start" else "sort_time DESC, is_public DESC, id DESC")
    with _conn._connect() as conn:
        rows = conn.execute(
            f"""
            SELECT name, reg_deadline, contest_time, mode, location, source_link, id
            FROM (
                SELECT name, official_reg_deadline AS reg_deadline,
                       official_time AS contest_time, '' AS mode, '' AS location,
                       official_site AS source_link, updated_at AS sort_time,
                       1 AS is_public, id
                FROM competitions c WHERE status = 'confirmed'
                  AND (
                    NOT EXISTS (
                        SELECT 1 FROM competition_notify_groups ng
                        WHERE ng.competition_id = c.id
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM weekly_contest_runs wr WHERE wr.competition_id = c.id
                    )
                    OR EXISTS (
                        SELECT 1 FROM competition_notify_groups ng2
                        WHERE ng2.competition_id = c.id AND ng2.group_id = ?
                    )
                  )
                UNION ALL
                SELECT name, reg_deadline, contest_time, mode, location,
                       source_link, created_at AS sort_time, 0 AS is_public, id
                FROM contests WHERE group_id = ?
            )
            ORDER BY {order_sql}
            LIMIT ?
            """,
            (group_id, group_id, limit),
        ).fetchall()
    return [dict(zip(fields, row)) for row in rows]


# LEGACY(P1): contests/reminders 旧表读写，仅供历史数据迁移与清理；新代码请用 competitions 新链。
def delete_contest(group_id: str, cid: int) -> bool:
    """按 id 删除某群的群级比赛（仅限本群记录）。"""
    with _conn._connect() as conn:
        cur = conn.execute(
            "DELETE FROM contests WHERE id = ? AND group_id = ?", (cid, group_id)
        )
        return cur.rowcount > 0


# LEGACY(P1): contests/reminders 旧表读写，仅供历史数据迁移与清理；新代码请用 competitions 新链。
def has_reminded(group_id: str, contest_id: int, remind_type: str, target_time: str) -> bool:
    """是否已对该（比赛, 类型, 目标时间）发过提醒。"""
    target_time = _conn._canonical_time(target_time)
    with _conn._connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM reminders WHERE group_id=? AND contest_id=? AND remind_type=? AND target_time=?",
            (group_id, contest_id, remind_type, target_time),
        ).fetchone()
        if row is not None:
            return True
        key = _training.delivery_dedup_key(group_id, "legacy_contest", contest_id, remind_type, target_time)
        return conn.execute("SELECT 1 FROM reminder_success_receipts WHERE dedup_key=?", (key,)).fetchone() is not None


# LEGACY(P1): contests/reminders 旧表读写，仅供历史数据迁移与清理；新代码请用 competitions 新链。
def mark_reminded(group_id: str, contest_id: int, remind_type: str, target_time: str) -> bool:
    """标记已提醒（发送成功后才可调用）。"""
    target_time = _conn._canonical_time(target_time)
    with _conn._connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO reminders (group_id, contest_id, remind_type, target_time, sent_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                group_id, contest_id, remind_type, target_time, _conn.now_str(),
            ),
        )
        return cur.rowcount > 0


_COMPETITION_FIELDS = (
    "name", "edition", "official_site", "official_reg_start",
    "official_reg_deadline", "official_time", "tracks", "official_rules",
    "device_req", "summary",
)


_COMPETITION_COLS = (
    "id, name, edition, official_site, official_reg_start, official_reg_deadline,"
    " official_time, tracks, official_rules, device_req, summary, status,"
    " verified_at, created_at, updated_at"
)


def _clean_competition_info(info: dict) -> dict:
    """清洗公共比赛字段：name 必填，时间字段统一格式，其余字符串。"""
    name = _conn._as_str(info.get("name"))
    if not name:
        raise ValueError("比赛名称不能为空")
    cleaned = {"name": name}
    for field in (
        "edition", "official_site", "tracks", "official_rules",
        "device_req", "summary",
    ):
        cleaned[field] = _conn._as_str(info.get(field) or "")
    for field in ("official_reg_start", "official_reg_deadline", "official_time"):
        cleaned[field] = _conn._clean_time(info.get(field))
    return cleaned


def _clean_competition_patch(info: dict, *, ignore_empty: bool = False) -> dict:
    """部分更新只清洗传入字段；自动提取的空值不表示删除。"""
    if not isinstance(info, dict):
        raise ValueError("比赛更新内容必须是字典")
    cleaned = {}
    for field, value in info.items():
        if field not in _COMPETITION_FIELDS:
            raise ValueError(f"不支持的字段：{field}")
        if ignore_empty and (value is None or isinstance(value, str) and not value.strip()):
            continue
        if field == "name":
            cleaned[field] = _conn._as_str(value)
            if not cleaned[field]:
                raise ValueError("比赛名称不能为空")
        elif field in ("official_reg_start", "official_reg_deadline", "official_time"):
            cleaned[field] = _conn._clean_time(value)
        else:
            cleaned[field] = _conn._as_str(value if value is not None else "")
        if field == "official_site" and cleaned[field]:
            parsed = urlsplit(cleaned[field])
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None or parsed.password is not None:
                raise ValueError("官网必须是无账号密码的 HTTP/HTTPS 链接")
    return cleaned


def _row_to_competition(r: sqlite3.Row) -> dict:
    return {
        "id": r[0], "name": r[1], "edition": r[2], "official_site": r[3],
        "official_reg_start": r[4], "official_reg_deadline": r[5],
        "official_time": r[6], "tracks": r[7], "official_rules": r[8],
        "device_req": r[9], "summary": r[10], "status": r[11],
        "verified_at": r[12], "created_at": r[13], "updated_at": r[14],
    }


def _set_competition_scope_on(conn: sqlite3.Connection, competition_id: int, group_ids) -> set[str]:
    groups = {str(g).strip() for g in (group_ids or []) if str(g).strip()}
    conn.execute("DELETE FROM competition_notify_groups WHERE competition_id = ?", (competition_id,))
    for group_id in sorted(groups):
        conn.execute(
            "INSERT OR IGNORE INTO competition_notify_groups (competition_id, group_id) VALUES (?, ?)",
            (competition_id, group_id),
        )
    return groups


def _create_competition_on(conn: sqlite3.Connection, info: dict,
                           status: str, operator: str, notify_groups=None) -> int:
    """在给定连接上创建公共比赛（供 create_competition 与同事务调用方复用）。"""
    cleaned = _clean_competition_info(info)
    if status not in ("draft", "confirmed"):
        raise ValueError("状态必须是 draft 或 confirmed")
    cur = conn.execute(
        "INSERT INTO competitions (name, edition, official_site, official_reg_start,"
        " official_reg_deadline, official_time, tracks, official_rules, device_req,"
        " summary, status, verified_at, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            cleaned["name"], cleaned["edition"], cleaned["official_site"],
            cleaned["official_reg_start"], cleaned["official_reg_deadline"],
            cleaned["official_time"], cleaned["tracks"], cleaned["official_rules"],
            cleaned["device_req"], cleaned["summary"], status,
            _conn.now_str() if status == "confirmed" else "",
            _conn.now_str(), _conn.now_str(),
        ),
    )
    cid = cur.lastrowid
    _add_version(conn, cid, "draft" if status == "draft" else "confirmed",
                 cleaned, "创建", operator)
    if status == "confirmed":
        if notify_groups is not None:
            groups = _set_competition_scope_on(conn, cid, notify_groups)
            _training._sync_contest_start_reminders(conn, cid, groups)
        else:
            _training._sync_contest_start_reminders(conn, cid)
    return cid


def create_competition(info: dict, status: str = "draft", operator: str = "",
                       conn: sqlite3.Connection | None = None) -> int:
    """创建公共比赛（默认草稿）。校验失败抛 ValueError，不写库。"""
    if conn is not None:
        return _create_competition_on(conn, info, status, operator)
    with _conn._connect() as conn:
        return _create_competition_on(conn, info, status, operator)


def create_weekly_competition(week_start: str, info: dict) -> tuple[int, bool]:
    """按北京时间周一日期幂等创建周赛，返回（比赛 id，是否新建）。

    周标识独立于名称与比赛时间；改名、修正或重启不改变本周生成记录。
    独占写事务确保多实例查重、比赛/版本创建与周记录写入原子完成。
    """
    week = datetime.strptime(week_start, "%Y-%m-%d")
    if week.weekday() != 0:
        raise ValueError("周赛周标识必须是周一日期")
    week_start = week.date().isoformat()
    with _conn._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT competition_id FROM weekly_contest_runs WHERE week_start = ?",
            (week_start,),
        ).fetchone()
        if existing is not None:
            return existing[0], False
        weekly_groups = _weekly_groups_on(conn)
        cid = _create_competition_on(conn, info, "confirmed", "weekly:auto", notify_groups=weekly_groups)
        conn.execute(
            "INSERT INTO weekly_contest_runs (week_start, competition_id, created_at)"
            " VALUES (?, ?, ?)",
            (week_start, cid, _conn.now_str()),
        )
    return cid, True


def _weekly_groups_on(conn: sqlite3.Connection) -> set[str]:
    groups = set(config.WEEKLY_CONTEST_GROUPS)
    for gid, enabled in conn.execute("SELECT group_id, enabled FROM group_weekly_settings"):
        if enabled:
            groups.add(gid)
        else:
            groups.discard(gid)
    return groups


def get_weekly_groups() -> set[str]:
    """配置文件作基线，每个群的显式开启/关闭持久覆盖该群，新增群不会继承。"""
    with _conn._connect() as conn:
        return _weekly_groups_on(conn)


def is_weekly_competition(competition_id: int) -> bool:
    with _conn._connect() as conn:
        return conn.execute("SELECT 1 FROM weekly_contest_runs WHERE competition_id=?", (competition_id,)).fetchone() is not None


def set_weekly_group_enabled(group_id: str, enabled: bool, operator: str = "") -> None:
    if group_id not in config.ALLOWED_GROUPS or type(enabled) is not bool:
        raise ValueError("请选择已授权群，并明确开启或关闭")
    with _conn._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO group_weekly_settings(group_id,enabled,updated_by,updated_at) VALUES (?,?,?,?)"
                     " ON CONFLICT(group_id) DO UPDATE SET enabled=excluded.enabled,"
                     " updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                     (group_id, int(enabled), operator, _conn.now_str()))
        groups = _weekly_groups_on(conn)
        # 改配置也立即更新未来已生成场次；不等待下周 cron，不补发过去节点。
        rows = conn.execute("SELECT c.id FROM competitions c JOIN weekly_contest_runs w ON w.competition_id=c.id"
                            " WHERE c.official_time>=?", (_training._delivery_now().strftime("%Y-%m-%d %H:%M"),)).fetchall()
        for (cid,) in rows:
            _set_competition_scope_on(conn, cid, groups)
            _training._sync_contest_start_reminders(conn, cid)
        _admin.add_audit(operator, group_id, "weekly_setting", "group", group_id, "", str(enabled), conn=conn)


def get_competition(cid: int) -> dict | None:
    with _conn._connect() as conn:
        row = conn.execute(
            f"SELECT {_COMPETITION_COLS} FROM competitions WHERE id = ?", (cid,)
        ).fetchone()
    return _row_to_competition(row) if row else None


def get_competition_notify_groups(competition_id: int) -> set[str]:
    """返回显式范围；计算实际可见/投递群应使用 get_competition_target_groups。"""
    with _conn._connect() as conn:
        rows = conn.execute(
            "SELECT group_id FROM competition_notify_groups WHERE competition_id = ?",
            (competition_id,),
        ).fetchall()
    return {r[0] for r in rows}


def _competition_target_groups_on(conn: sqlite3.Connection, competition_id: int) -> set[str]:
    groups = {r[0] for r in conn.execute(
        "SELECT group_id FROM competition_notify_groups WHERE competition_id=?", (competition_id,),
    )}
    if groups:
        return groups & set(config.ALLOWED_GROUPS)
    if (conn.execute("SELECT 1 FROM weekly_contest_runs WHERE competition_id=?", (competition_id,)).fetchone()
            or not conn.execute("SELECT 1 FROM competitions WHERE id=?", (competition_id,)).fetchone()):
        return set()
    return set(config.ALLOWED_GROUPS)


def get_competition_target_groups(competition_id: int) -> set[str]:
    """公共比赛可共享；自动周赛只对明确指定的群可见，空范围绝不回退全群。"""
    with _conn._connect() as conn:
        return _competition_target_groups_on(conn, competition_id)


def list_group_competitions(group_id: str, status: str | None = "confirmed") -> list[dict]:
    """列出对指定群可见的比赛（公共比赛对全部允许群可见，周赛仅指定群可见）。"""
    gid = str(group_id).strip()
    return [
        competition for competition in list_competitions(status)
        if gid in get_competition_target_groups(competition["id"])
    ]


def set_competition_notify_groups(competition_id: int, group_ids,
                                  operator: str = "") -> int:
    """设置范围；公共比赛空集合为全部允许群，自动周赛空集合为无目标群。

    Returns:
        实际写入的群数（0 表示已清空为全局）。
    """
    groups = {str(g).strip() for g in (group_ids or []) if str(g).strip()}
    with _conn._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM competition_notify_groups WHERE competition_id = ?",
            (competition_id,),
        )
        for gid in sorted(groups):
            conn.execute(
                "INSERT OR IGNORE INTO competition_notify_groups"
                " (competition_id, group_id) VALUES (?, ?)",
                (competition_id, gid),
            )
        _training._sync_contest_start_reminders(conn, competition_id, groups or set(config.ALLOWED_GROUPS))
        _admin.add_audit(operator, "", "set_notify_groups", "competition",
                  str(competition_id), "", ",".join(sorted(groups)), conn=conn)
        return len(groups)


def list_competitions(status: str | None = None) -> list[dict]:
    """列出公共比赛；status 可选过滤（draft/confirmed）。"""
    with _conn._connect() as conn:
        if status is None:
            rows = conn.execute(
                f"SELECT {_COMPETITION_COLS} FROM competitions ORDER BY id DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_COMPETITION_COLS} FROM competitions WHERE status = ? ORDER BY id DESC",
                (status,),
            ).fetchall()
    return [_row_to_competition(r) for r in rows]


def _add_version(conn: sqlite3.Connection, cid: int, kind: str, fields: dict,
                 note: str, operator: str) -> None:
    conn.execute(
        "INSERT INTO competition_versions (competition_id, kind, fields_json, change_note, created_by, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (cid, kind, json.dumps(fields, ensure_ascii=False), note, operator or "", _conn.now_str()),
    )


def update_competition(cid: int, info: dict, operator: str = "", note: str = "") -> bool:
    """更新公共比赛字段并记录变更版本。仅更新传入的字段（部分更新）。

    传入字段逐个清洗：name 非空；时间字段统一格式；其余字符串。
    """
    cleaned: dict[str, str] = {}
    allowed = {
        "name", "edition", "official_site", "official_reg_start",
        "official_reg_deadline", "official_time", "tracks", "official_rules",
        "device_req", "summary",
    }
    for field in info:
        if field not in allowed:
            raise ValueError(f"不支持的字段：{field}")
        if field == "name":
            name = _conn._as_str(info[field])
            if not name:
                raise ValueError("比赛名称不能为空")
            cleaned[field] = name
        elif field in ("official_reg_start", "official_reg_deadline", "official_time"):
            cleaned[field] = _conn._clean_time(info[field])
        else:
            cleaned[field] = _conn._as_str(info[field] or "")
    cols = list(cleaned)
    if not cols:
        return False
    sets = ", ".join(f"{c} = ?" for c in cols)
    args = [cleaned[c] for c in cols] + [_conn.now_str(), cid]
    with _conn._connect() as conn:
        cur = conn.execute(
            f"UPDATE competitions SET {sets}, updated_at = ? WHERE id = ?", args
        )
        if cur.rowcount == 0:
            return False
        cur_comp = conn.execute(
            f"SELECT {_COMPETITION_COLS} FROM competitions WHERE id = ?", (cid,)
        ).fetchone()
        _add_version(conn, cid, "draft", _row_to_competition(cur_comp),
                     note or "编辑", operator)
        if "official_time" in cleaned:
            scoped = {r[0] for r in conn.execute(
                "SELECT group_id FROM competition_notify_groups WHERE competition_id = ?", (cid,)
            ).fetchall()}
            _training._sync_contest_start_reminders(conn, cid, scoped or None)
        return True


def confirm_competition(cid: int, operator: str = "") -> bool:
    """老师确认草稿：status → confirmed，记录 confirmed 版本与核验时间。"""
    with _conn._connect() as conn:
        cur = conn.execute(
            "UPDATE competitions SET status = 'confirmed', verified_at = ?, updated_at = ?"
            " WHERE id = ? AND status != 'confirmed'",
            (_conn.now_str(), _conn.now_str(), cid),
        )
        if cur.rowcount == 0:
            return False
        row = conn.execute(
            f"SELECT {_COMPETITION_COLS} FROM competitions WHERE id = ?", (cid,)
        ).fetchone()
        _add_version(conn, cid, "confirmed", _row_to_competition(row), "确认发布", operator)
        scoped = {r[0] for r in conn.execute(
            "SELECT group_id FROM competition_notify_groups WHERE competition_id = ?", (cid,)
        ).fetchall()}
        _training._sync_contest_start_reminders(conn, cid, scoped or None)
        return True


def reject_competition(cid: int, operator: str = "", note: str = "") -> bool:
    """老师拒绝草稿：保持 draft 并记录拒绝说明（不删除数据）。"""
    with _conn._connect() as conn:
        row = conn.execute(
            f"SELECT {_COMPETITION_COLS} FROM competitions WHERE id = ?", (cid,)
        ).fetchone()
        if row is None:
            return False
        _add_version(conn, cid, "draft", _row_to_competition(row),
                     f"拒绝：{note or '未说明原因'}", operator)
        return True


def set_competition_status(cid: int, status: str, operator: str = "",
                           note: str = "") -> bool:
    """切换公共比赛状态（如 confirmed → draft 待重新审核），并记录版本。"""
    if status not in ("draft", "confirmed"):
        raise ValueError("状态必须是 draft 或 confirmed")
    with _conn._connect() as conn:
        cur = conn.execute(
            "UPDATE competitions SET status = ?, verified_at = CASE WHEN ? = 'confirmed'"
            " THEN ? ELSE verified_at END, updated_at = ? WHERE id = ? AND status != ?",
            (status, status, _conn.now_str(), _conn.now_str(), cid, status),
        )
        if cur.rowcount == 0:
            return False
        row = conn.execute(
            f"SELECT {_COMPETITION_COLS} FROM competitions WHERE id = ?", (cid,)
        ).fetchone()
        _add_version(conn, cid, status, _row_to_competition(row),
                     note or f"状态切换为 {status}", operator)
        scoped = {r[0] for r in conn.execute(
            "SELECT group_id FROM competition_notify_groups WHERE competition_id = ?", (cid,)
        ).fetchall()}
        _training._sync_contest_start_reminders(conn, cid, scoped or None)
        return True


def list_competition_versions(cid: int) -> list[dict]:
    with _conn._connect() as conn:
        rows = conn.execute(
            "SELECT id, competition_id, kind, fields_json, change_note, created_by, created_at"
            " FROM competition_versions WHERE competition_id = ? ORDER BY id DESC",
            (cid,),
        ).fetchall()
    return [
        {
            "id": r[0], "competition_id": r[1], "kind": r[2],
            "fields_json": r[3], "change_note": r[4], "created_by": r[5],
            "created_at": r[6],
        }
        for r in rows
    ]


def _create_confirmed_from_pending(info: dict, source_url: str, source_text: str,
                                   operator: str, *, conn: sqlite3.Connection | None = None,
                                   notify_groups=None) -> tuple[int, bool]:
    """无链接发布及超管确认落库：按「名称+届次+时间」原子去重。

    不要求 source_url，因此群消息转发与官网发布的同场比赛仍可判重；
    免审发布缺少链接时也复用此路径，不注册空地址监控来源。
    """
    if not isinstance(info, dict):
        raise ValueError("比赛信息必须是对象")
    cleaned = _clean_competition_info(info)
    source_url = _conn._as_str(source_url)

    def create_on(active_conn: sqlite3.Connection) -> tuple[int, bool]:
        # 事务内查重，避免并发同意同一条申请造成重复入库。
        rows = active_conn.execute(
            f"SELECT {_COMPETITION_COLS} FROM competitions WHERE status IN ('confirmed', 'draft')"
        ).fetchall()
        for row in rows:
            record = _row_to_competition(row)
            if (str(record.get("name") or "").strip().lower()
                    == str(cleaned.get("name") or "").strip().lower()
                    and str(record.get("edition") or "").strip().lower()
                    == str(cleaned.get("edition") or "").strip().lower()
                    and str(record.get("official_time") or "").strip().lower()
                    == str(cleaned.get("official_time") or "").strip().lower()):
                return record["id"], False
        cid = _create_competition_on(active_conn, cleaned, "confirmed", operator,
                                     notify_groups=notify_groups)
        if source_url:
            sid = _sources.add_competition_source(cid, source_url, conn=active_conn)
            body_hash = hashlib.sha256((source_text or "").encode("utf-8")).hexdigest()
            _sources.update_source_fetch(sid, body_hash=body_hash, last_status="confirmed", conn=active_conn)
        return cid, True

    if conn is not None:
        return create_on(conn)
    with _conn._connect() as active_conn:
        active_conn.execute("BEGIN IMMEDIATE")
        return create_on(active_conn)


def create_published_competition_once(info: dict, source_url: str, source_text: str,
                                     operator: str = "", *, source_group_id: str = "") -> tuple[int, bool]:
    """原子发布，返回（比赛 id，是否新建）；重复不改资料或成功正文基线。

    持久发布身份独立于后续手工修正；写事务内完成查重、来源和版本创建。
    """
    source_url = _conn._as_str(source_url)
    if not isinstance(source_text, str):
        raise ValueError("来源正文必须是字符串")
    if not isinstance(info, dict):
        raise ValueError("比赛信息必须是对象")
    notify_groups = {source_group_id} if config.PUBLISH_SCOPE_TO_SOURCE and source_group_id else None
    if not source_url:
        return _create_confirmed_from_pending(info, "", source_text, operator,
                                               notify_groups=notify_groups)
    cleaned = _clean_competition_info({**info, "official_site": source_url})
    canonical_url = _sources._canonical_publication_url(source_url)
    identity = _admin._publication_identity(cleaned, canonical_url)
    body_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    with _conn._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT competition_id FROM publication_claims WHERE identity_key = ?",
            (identity,),
        ).fetchone()
        if existing is not None:
            return existing[0], False
        cid = _sources._find_legacy_publication(conn, identity, canonical_url)
        if cid is not None:
            conn.execute(
                "INSERT INTO publication_claims (identity_key, competition_id, created_at)"
                " VALUES (?, ?, ?)", (identity, cid, _conn.now_str()),
            )
            return cid, False
        cid = _create_competition_on(conn, cleaned, "confirmed", operator,
                                     notify_groups=notify_groups)
        sid = _sources.add_competition_source(cid, source_url, conn=conn)
        _sources.update_source_fetch(
            sid, body_hash=body_hash, last_status="confirmed", conn=conn,
        )
        conn.execute(
            "INSERT INTO publication_claims (identity_key, competition_id, created_at)"
            " VALUES (?, ?, ?)", (identity, cid, _conn.now_str()),
        )
        return cid, True


def create_published_competition(info: dict, source_url: str, source_text: str,
                                operator: str = "") -> int:
    """兼容原返回值的发布入口，重复发布仍返回同一个比赛 id。"""
    cid, _ = create_published_competition_once(info, source_url, source_text, operator)
    return cid


def _normalize_question(question: str) -> str:
    """只规范空白；不改变标点、大小写或按关键词猜测同题。"""
    return " ".join(_conn._as_str(question).split())


def mark_question_answered(qid: int, faq_id: int = 0) -> bool:
    """问题已被回答（合并进 FAQ 后标记 answered）。"""
    with _conn._connect() as conn:
        cur = conn.execute(
            "UPDATE unanswered_questions SET status = 'answered', faq_id = ? WHERE id = ?",
            (faq_id, qid),
        )
        return cur.rowcount > 0


def amend_competition(cid: int, info: dict, operator: str = "", note: str = "", *,
                      source_text: str | None = None) -> bool:
    """超管修正已发布比赛（v2 修正流程，免审）。

    - 直接更新指定字段（校验同 update_competition）
    - 记录 kind='amend' 的修正版本
    - 清除该比赛未应用的待审更新（人工修正以最新信息为准）
    - 写入审计
    """
    cleaned = _clean_competition_patch(info)
    if source_text is not None and (
        not isinstance(source_text, str) or not source_text.strip() or not cleaned.get("official_site")
    ):
        raise ValueError("重新提取必须提供官网链接和有效正文")
    cols = list(cleaned)
    if not cols:
        return False
    sets = ", ".join(f"{c} = ?" for c in cols)
    args = [cleaned[c] for c in cols] + [_conn.now_str(), cid]
    with _conn._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            f"UPDATE competitions SET {sets}, updated_at = ? WHERE id = ?", args
        )
        if cur.rowcount == 0:
            return False
        if "official_site" in cleaned:
            _sources._sync_competition_source_on(conn, cid, cleaned["official_site"], source_text)
        cur_comp = conn.execute(
            f"SELECT {_COMPETITION_COLS} FROM competitions WHERE id = ?", (cid,)
        ).fetchone()
        _add_version(conn, cid, "amend", _row_to_competition(cur_comp),
                     note or "超管修正", operator)
        conn.execute(
            "DELETE FROM competition_pending_updates WHERE competition_id = ?", (cid,)
        )
        _admin.add_audit(operator, "", "amend", "competition", str(cid),
                  "", json.dumps(cleaned, ensure_ascii=False), conn=conn)
        if "official_time" in cleaned:
            scoped = {r[0] for r in conn.execute(
                "SELECT group_id FROM competition_notify_groups WHERE competition_id = ?", (cid,)
            ).fetchall()}
            _training._sync_contest_start_reminders(conn, cid, scoped or None)
        return True


def list_due_contest_start_deliveries(now: datetime, allowed_groups: set[str]) -> list[dict]:
    """列出到期且仍在宽限期的持久比赛开赛提醒；不创建迟到节点。"""
    current = _training._delivery_now(now).strftime("%Y-%m-%d %H:%M:%S")
    groups = sorted(allowed_groups)
    if not groups:
        return []
    with _conn._connect() as conn:
        rows = conn.execute(
            f"SELECT {_training._DELIVERY_COLS} FROM reminder_deliveries"
            " WHERE obj_type = 'competition' AND remind_type = 'contest_start'"
            " AND status IN ('pending', 'sending') AND scheduled_at <= ? AND expires_at >= ?"
            f" AND group_id IN ({','.join('?' for _ in groups)}) ORDER BY scheduled_at, id",
            [current, current, *groups],
        ).fetchall()
    return [_training._row_to_delivery(r) for r in rows]


def reconcile_deliveries(now: datetime | None = None, *, max_attempts: int = 3,
                         allowed_groups: set[str] | None = None) -> None:
    """终结旧计划，恢复过期租约；活跃发送者保留其回执写入权。"""
    now = _training._delivery_now(now)
    with _conn._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            f"SELECT {_training._DELIVERY_COLS} FROM reminder_deliveries"
            " WHERE status IN ('pending', 'sending', 'cancelled')"
            " AND NOT EXISTS (SELECT 1 FROM reminder_success_receipts r"
            " WHERE r.dedup_key = reminder_deliveries.dedup_key)"
        ).fetchall()
        for row in rows:
            delivery = _training._row_to_delivery(row)
            if delivery["remind_type"] == "contest_start" and delivery["status"] == "cancelled":
                continue
            # 在途发送仍有写回权；对象关闭/改期只能阻止尚未发送的计划。
            if delivery["status"] == "sending":
                until = _training._delivery_parse_time(delivery["lease_until"])
                if until is not None and until > now and delivery["lease_token"]:
                    continue
            status = _training._delivery_invalid_status(conn, delivery, now, allowed_groups)
            if status is None:
                if delivery["attempts"] >= max_attempts:
                    status = "failed"
                elif delivery["status"] in ("sending", "cancelled"):
                    status = "pending"
            if status is not None:
                conn.execute(
                    "UPDATE reminder_deliveries SET status = ?, lease_token = '', lease_until = '' WHERE id = ?",
                    (status, delivery["id"]),
                )


def list_pending_deliveries(limit: int = 200) -> list[dict]:
    with _conn._connect() as conn:
        rows = conn.execute(
            f"SELECT {_training._DELIVERY_COLS} FROM reminder_delivery_states"
            " WHERE status = 'pending' ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
    return [_training._row_to_delivery(r) for r in rows]


def list_deliveries(group_id: str, *, statuses: tuple[str, ...] | None = None,
                    limit: int = 200, order: str = "id") -> list[dict]:
    """列出某群投递记录。

    order="id"：按主键倒序（发送/巡检链路沿用，保持既有行为）；
    order="schedule"：按预计触发时间正序（同时间按 id 正序），供展示层从近到远排列。
    """
    where = "group_id = ?"
    args: list = [group_id]
    if statuses:
        where += " AND status IN (" + ",".join("?" for _ in statuses) + ")"
        args.extend(statuses)
    # scheduled_at 为空的记录排在最后，避免空串抢占队首。
    order_by = ("scheduled_at = '', scheduled_at, id" if order == "schedule" else "id DESC")
    with _conn._connect() as conn:
        rows = conn.execute(
            f"SELECT {_training._DELIVERY_COLS} FROM reminder_delivery_states WHERE {where} ORDER BY {order_by} LIMIT ?",
            [*args, limit],
        ).fetchall()
    return [_training._row_to_delivery(row) for row in rows]


_TARGET_NAME_COLUMNS = {
    "competition": ("competitions", "name"),
    "collection": ("collections", "title"),
    "training_session": ("training_sessions", "title"),
}


_GROUP_PERMISSION_RETRY_SECONDS = 300


def drop_pending_deliveries(obj_type: str, obj_id: int, *, group_id: str = "") -> int:
    """作废某对象的**未发送**提醒节点，返回删除条数。

    课程改期/取消、比赛改期后，旧时间点的 pending 行已经不可能再被
    ``build_*_reminders`` 命中（扫描式计划只按最新数据重建），留着只会让
    「待发清单」显示出并不存在的节点，因此在改期时清掉。
    只删 ``status='pending'``：``sending`` 行可能正被别的进程持有租约，
    删除会破坏其回执写入，交给 ``reconcile_deliveries`` 自然收尾。
    """
    try:
        oid = int(obj_id)
    except (TypeError, ValueError):
        return 0
    sql = ("DELETE FROM reminder_deliveries WHERE obj_type = ? AND obj_id = ?"
           " AND status = 'pending'")
    args: list = [_conn._as_str(obj_type), oid]
    if _conn._as_str(group_id):
        sql += " AND group_id = ?"
        args.append(_conn._as_str(group_id))
    with _conn._connect() as conn:
        cur = conn.execute(sql, args)
        return cur.rowcount
