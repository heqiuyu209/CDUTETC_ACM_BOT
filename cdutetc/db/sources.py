"""来源监控与发布审核管线：competition_sources、待审发布、爬虫 diff 待应用更新。"""

import logging
import hashlib
import json
import sqlite3
from urllib.parse import urlsplit
from cdutetc import config

from cdutetc.db import admin as _admin
from cdutetc.db import connection as _conn
from cdutetc.db import contests as _contests
from cdutetc.db import training as _training

logger = logging.getLogger(__name__)


_SOURCE_COLS = (
    "id, competition_id, url, etag, last_modified, body_hash,"
    " last_fetched_at, last_status, last_error, created_at, active, revision"
)


def _row_to_source(r: sqlite3.Row) -> dict:
    return {
        "id": r[0], "competition_id": r[1], "url": r[2], "etag": r[3],
        "last_modified": r[4], "body_hash": r[5], "last_fetched_at": r[6],
        "last_status": r[7], "last_error": r[8], "created_at": r[9], "active": bool(r[10]),
        "revision": r[11],
    }


def add_competition_source(cid: int, url: str,
                           conn: sqlite3.Connection | None = None) -> int:
    """为公共比赛添加来源 URL（同 URL 唯一，重复返回已存在 id）。"""
    url = _conn._as_str(url)
    if not url:
        raise ValueError("来源 URL 不能为空")
    sql_ins = (
        "INSERT OR IGNORE INTO competition_sources (competition_id, url, created_at)"
        " VALUES (?, ?, ?)"
    )
    sql_sel = (
        "SELECT id FROM competition_sources WHERE competition_id = ? AND url = ?"
    )
    if conn is not None:
        cur = conn.execute(sql_ins, (cid, url, _conn.now_str()))
        if cur.rowcount > 0:
            return cur.lastrowid
        return conn.execute(sql_sel, (cid, url)).fetchone()[0]
    with _conn._connect() as conn:
        cur = conn.execute(sql_ins, (cid, url, _conn.now_str()))
        if cur.rowcount > 0:
            return cur.lastrowid
        row = conn.execute(sql_sel, (cid, url)).fetchone()
        return row[0]


_SOURCE_SNAPSHOT_FIELDS = (
    "id", "competition_id", "url", "etag", "last_modified", "body_hash",
    "last_fetched_at", "last_status", "last_error", "active", "revision",
)


def _source_snapshot_condition(source: dict) -> tuple[str, list]:
    defaults = {"active": True, "revision": 0}
    return (
        " AND ".join(f"{field} = ?" for field in _SOURCE_SNAPSHOT_FIELDS),
        [source.get(field, defaults.get(field, "")) for field in _SOURCE_SNAPSHOT_FIELDS],
    )


def source_snapshot_matches(source: dict, conn: sqlite3.Connection | None = None) -> bool:
    """检查来源标识、有效性和整份抓取基线快照；事务调用方传入同一连接。"""
    condition, values = _source_snapshot_condition(source)
    query = f"SELECT 1 FROM competition_sources WHERE active = 1 AND {condition}"
    if conn is not None:
        return conn.execute(query, values).fetchone() is not None
    with _conn._connect() as conn:
        return conn.execute(query, values).fetchone() is not None


def update_source_fetch(source_id: int, *, etag: str = "", last_modified: str = "",
                        body_hash: str = "", last_status: str = "",
                        last_error: str = "", last_fetched_at: str | None = None,
                        conn: sqlite3.Connection | None = None,
                        expected_source: dict | None = None) -> bool:
    """条件更新有效来源；expected_source 过期时零写入，返回 False。"""
    if last_fetched_at is None:
        last_fetched_at = _conn.now_str()
    sql = (
        "UPDATE competition_sources SET etag = ?, last_modified = ?, body_hash = ?,"
        " last_fetched_at = ?, last_status = ?, last_error = ?, revision = revision + 1 WHERE id = ? AND active = 1"
    )
    values = (etag, last_modified, body_hash, last_fetched_at, last_status, last_error, source_id)
    if expected_source is not None:
        condition, expected_values = _source_snapshot_condition(expected_source)
        sql += f" AND {condition}"
        values += tuple(expected_values)
    if conn is not None:
        return conn.execute(sql, values).rowcount == 1
    with _conn._connect() as conn:
        return conn.execute(sql, values).rowcount == 1


def get_source_by_url(url: str, competition_id: int | None = None) -> dict | None:
    """查询有效来源；指定比赛时不借用其他比赛相同 URL 的基线。"""
    where = "url = ? AND active = 1"
    args = [url]
    if competition_id is not None:
        where += " AND competition_id = ?"
        args.append(competition_id)
    with _conn._connect() as conn:
        row = conn.execute(
            f"SELECT {_SOURCE_COLS} FROM competition_sources WHERE {where} ORDER BY id DESC LIMIT 1", args,
        ).fetchone()
    return _row_to_source(row) if row else None


def list_sources(competition_id: int | None = None, *, active_only: bool = True) -> list[dict]:
    """抓取默认只读有效来源；active_only=False 用于查看保留的来源历史。"""
    conditions = ["active = 1"] if active_only else []
    args = []
    if competition_id is not None:
        conditions.append("competition_id = ?")
        args.append(competition_id)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    with _conn._connect() as conn:
        rows = conn.execute(
            f"SELECT {_SOURCE_COLS} FROM competition_sources{where} ORDER BY id DESC", args,
        ).fetchall()
    return [_row_to_source(r) for r in rows]


def _sync_competition_source_on(conn: sqlite3.Connection, cid: int, url: str,
                                source_text: str | None = None) -> None:
    """在比赛事务中切换有效官网；只有真实抓取的正文才建立新基线。"""
    conn.execute(
        "UPDATE competition_sources SET active = 0, revision = revision + 1"
        " WHERE competition_id = ? AND url != ? AND active = 1", (cid, url),
    )
    if not url:
        return
    sid = add_competition_source(cid, url, conn=conn)
    conn.execute("UPDATE competition_sources SET active = 1, revision = revision + 1 WHERE id = ? AND active = 0", (sid,))
    if source_text is not None:
        update_source_fetch(
            sid, body_hash=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
            last_status="confirmed", conn=conn,
        )


def _canonical_publication_url(url: str) -> str:
    """仅归一化协议、主机、默认端口和 fragment，保留 path/query 原义。"""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = parts.hostname
    if scheme not in ("http", "https") or not host:
        raise ValueError("来源 URL 必须是有效 HTTP(S) 地址")
    host = host.lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    if port is not None and port != {"http": 80, "https": 443}[scheme]:
        host += f":{port}"
    # 若地址含 userinfo，其大小写保留；只归一化主机。
    if "@" in parts.netloc:
        host = parts.netloc.rsplit("@", 1)[0] + "@" + host
    result = f"{scheme}://{host}{parts.path}"
    if "?" in url.split("#", 1)[0]:
        result += "?" + parts.query
    return result


def find_duplicate_competition(info: dict) -> dict | None:
    """按「名称 + 届次 + 比赛时间」三者完全一致判定重复。

    与 _publication_identity 不同：本函数不看链接，因此转发来的群消息
    （通常没有链接）也能查出重复；三者任一不同都不算重复，避免误拒。

    Returns:
        命中的比赛记录（含 id/name/edition/official_time/status），无则 None。
    """
    if not isinstance(info, dict):
        return None
    def norm(value) -> str:
        return " ".join(str(value or "").split()).strip().lower()

    name = norm(info.get("name"))
    if not name:
        return None
    edition = norm(info.get("edition"))
    contest_time = norm(info.get("official_time"))
    with _conn._connect() as conn:
        rows = conn.execute(
            f"SELECT {_contests._COMPETITION_COLS} FROM competitions"
            " WHERE status IN ('confirmed', 'draft')"
        ).fetchall()
    for row in rows:
        record = _contests._row_to_competition(row)
        if (
            norm(record.get("name")) == name
            and norm(record.get("edition")) == edition
            and norm(record.get("official_time")) == contest_time
        ):
            return record
    return None


def _find_legacy_publication(conn: sqlite3.Connection, identity: str,
                             canonical_url: str) -> int | None:
    """给旧库补发布身份；当前资料或初始正式创建版本必须精确匹配。"""
    claimed_ids = {row[0] for row in conn.execute("SELECT competition_id FROM publication_claims")}
    sources: dict[int, list[str]] = {}
    for cid, url in conn.execute("SELECT competition_id, url FROM competition_sources"):
        sources.setdefault(cid, []).append(url)
    original_versions: dict[int, list[dict]] = {}
    for cid, raw in conn.execute(
        "SELECT competition_id, fields_json FROM competition_versions"
        " WHERE kind = 'confirmed' AND change_note = '创建'"
    ):
        try:
            fields = json.loads(raw)
        except (ValueError, TypeError, RecursionError):
            continue
        if isinstance(fields, dict):
            original_versions.setdefault(cid, []).append(fields)
    for row in conn.execute(f"SELECT {_contests._COMPETITION_COLS} FROM competitions ORDER BY id"):
        competition = _contests._row_to_competition(row)
        cid = competition["id"]
        if cid in claimed_ids:
            # 已有持久身份的记录不再从可编辑字段推导额外身份。
            continue
        candidates = list(original_versions.get(cid, []))
        if competition["status"] == "confirmed":
            candidates.append(competition)
        for candidate in candidates:
            try:
                cleaned = _contests._clean_competition_info(candidate)
                if _admin._publication_identity(cleaned, canonical_url) != identity:
                    continue
            except ValueError:
                continue
            urls = sources.get(cid, []) + [cleaned["official_site"]]
            for url in urls:
                try:
                    if _canonical_publication_url(url) == canonical_url:
                        return cid
                except ValueError:
                    continue
    return None


_PENDING_PUB_COLS = (
    "id, group_id, name, edition, official_time, fields_json, source_url,"
    " source_text, submitted_by, submitted_at, duplicate_id, duplicate_note,"
    " status, decided_by, decided_at"
)


def _row_to_pending_publication(r: sqlite3.Row) -> dict:
    return {
        "id": r[0], "group_id": r[1], "name": r[2], "edition": r[3],
        "official_time": r[4], "fields_json": r[5], "source_url": r[6],
        "source_text": r[7], "submitted_by": r[8], "submitted_at": r[9],
        "duplicate_id": r[10], "duplicate_note": r[11], "status": r[12],
        "decided_by": r[13], "decided_at": r[14],
    }


def create_pending_publication(info: dict, group_id: str, source_url: str,
                               source_text: str, submitted_by: str,
                               duplicate: dict | None = None) -> int:
    """登记一条待超管确认的发布申请；此时不写 competitions，群内不可见。"""
    if not isinstance(info, dict):
        raise ValueError("比赛信息必须是对象")

    def opt_str(value) -> str:
        """可选字段：缺失/None 视为空串，非字符串仍按严格类型报错。"""
        return "" if value is None else _conn._as_str(value)

    duplicate_id = int(duplicate["id"]) if duplicate else 0
    duplicate_note = ""
    if duplicate:
        duplicate_note = (
            f"已存在同名同届次同时间的记录（编号 {duplicate_id}，"
            f"状态 {duplicate.get('status') or '未知'}）"
        )
    with _conn._connect() as conn:
        cur = conn.execute(
            "INSERT INTO pending_publications (group_id, name, edition, official_time,"
            " fields_json, source_url, source_text, submitted_by, submitted_at,"
            " duplicate_id, duplicate_note, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
            (
                opt_str(group_id),
                opt_str(info.get("name")),
                opt_str(info.get("edition")),
                opt_str(info.get("official_time")),
                json.dumps(info, ensure_ascii=False),
                opt_str(source_url),
                opt_str(source_text),
                opt_str(submitted_by),
                _conn.now_str(),
                duplicate_id,
                duplicate_note,
            ),
        )
        return cur.lastrowid


def get_pending_publication(pid: int) -> dict | None:
    with _conn._connect() as conn:
        row = conn.execute(
            f"SELECT {_PENDING_PUB_COLS} FROM pending_publications WHERE id = ?", (pid,)
        ).fetchone()
    return _row_to_pending_publication(row) if row else None


def list_pending_publications(status: str = "pending") -> list[dict]:
    """列出发布申请；status 传空串则返回全部。"""
    with _conn._connect() as conn:
        if status:
            rows = conn.execute(
                f"SELECT {_PENDING_PUB_COLS} FROM pending_publications"
                " WHERE status = ? ORDER BY id DESC", (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_PENDING_PUB_COLS} FROM pending_publications ORDER BY id DESC"
            ).fetchall()
    return [_row_to_pending_publication(r) for r in rows]


def approve_pending_publication(pid: int, operator: str = "") -> tuple[int, bool]:
    """超管同意：原子创建正式比赛、群级提醒计划并提交申请状态。"""
    with _conn._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"SELECT {_PENDING_PUB_COLS} FROM pending_publications WHERE id = ?", (pid,)
        ).fetchone()
        if row is None:
            raise ValueError(f"发布申请 #{pid} 不存在")
        pending = _row_to_pending_publication(row)
        if pending["status"] == "approved":
            info = json.loads(pending["fields_json"] or "{}")
            cleaned = _contests._clean_competition_info(info)
            found = conn.execute(
                "SELECT id FROM competitions WHERE lower(name) = lower(?)"
                " AND lower(edition) = lower(?) AND lower(official_time) = lower(?)"
                " ORDER BY id LIMIT 1",
                (cleaned["name"], cleaned["edition"], cleaned["official_time"]),
            ).fetchone()
            if found:
                return found[0], False
            raise ValueError(f"已批准申请 #{pid} 对应比赛记录不存在")
        if pending["status"] != "pending":
            raise ValueError(f"发布申请 #{pid} 已是 {pending['status']} 状态")
        info = json.loads(pending["fields_json"] or "{}")
        pending_groups = (
            {pending["group_id"]}
            if config.PUBLISH_SCOPE_TO_SOURCE and pending["group_id"] else set()
        )
        cid, created = _contests._create_confirmed_from_pending(
            info, pending["source_url"], pending["source_text"],
            operator=f"approve:{operator or pending['submitted_by']}",
            conn=conn, notify_groups=pending_groups,
        )
        if not created:
            existing_groups = {r[0] for r in conn.execute(
                "SELECT group_id FROM competition_notify_groups WHERE competition_id = ?", (cid,)
            ).fetchall()}
            if config.PUBLISH_SCOPE_TO_SOURCE and pending["group_id"]:
                pending_groups = existing_groups | {pending["group_id"]}
            else:
                pending_groups = set()
            groups = _contests._set_competition_scope_on(conn, cid, pending_groups)
            _training._sync_contest_start_reminders(conn, cid, groups or None)
        cur = conn.execute(
            "UPDATE pending_publications SET status = 'approved', decided_by = ?, decided_at = ?"
            " WHERE id = ? AND status = 'pending'",
            (_conn._as_str(operator), _conn.now_str(), pid),
        )
        if cur.rowcount != 1:
            raise ValueError(f"发布申请 #{pid} 状态已变化")
        _admin.add_audit(operator, pending["group_id"], "approve_publication",
                  "pending_publication", str(pid), "", f"competition:{cid}", conn=conn)
        return cid, created


def reject_pending_publication(pid: int, operator: str = "", note: str = "") -> bool:
    """超管拒绝：标记已拒绝，不写任何比赛数据。"""
    pending = get_pending_publication(pid)
    if pending is None or pending["status"] != "pending":
        return False
    with _conn._connect() as conn:
        conn.execute(
            "UPDATE pending_publications SET status = 'rejected', decided_by = ?,"
            " decided_at = ? WHERE id = ?", (_conn._as_str(operator), _conn.now_str(), pid),
        )
        _admin.add_audit(operator, pending["group_id"], "reject_publication",
                  "pending_publication", str(pid), "", note or "超管拒绝", conn=conn)
    return True


# LEGACY(P1): contests/reminders 旧表读写，仅供历史数据迁移与清理；新代码请用 competitions 新链。
def list_unclaimed_contests() -> list[dict]:
    """未归属任何群的旧记录（group_id=''），仅管理员可见并显式迁移。"""
    with _conn._connect() as conn:
        rows = conn.execute(
            f"SELECT {_contests._CONTEST_COLS} FROM contests WHERE group_id = '' ORDER BY id"
        ).fetchall()
    return [_contests._row_to_contest(r) for r in rows]


# LEGACY(P1): contests/reminders 旧表读写，仅供历史数据迁移与清理；新代码请用 competitions 新链。
def claim_contest(contest_id: int, group_id: str) -> bool:
    """将旧记录显式归属到指定群。"""
    group_id = _conn._as_str(group_id)
    with _conn._connect() as conn:
        cur = conn.execute(
            "UPDATE contests SET group_id = ? WHERE id = ? AND group_id = ''",
            (group_id, contest_id),
        )
        return cur.rowcount > 0


def migrate_contest_to_competition(contest_id: int, group_id: str,
                                   operator: str = "") -> int | None:
    """把一条旧群级比赛记录升级为公共比赛（数据不删除）。

    迁移后原 contests 记录归属到该群（不再留在待迁移区），
    新公共比赛进入草稿状态，由老师 /修正 或后续抓取推进正式化。
    全部写操作在同一连接同一事务内完成，避免跨连接锁库。
    """
    with _conn._connect() as conn:
        row = conn.execute(
            f"SELECT {_contests._CONTEST_COLS} FROM contests WHERE id = ?", (contest_id,)
        ).fetchone()
        if row is None:
            return None
        contest = _contests._row_to_contest(row)
        comp_id = _contests._create_competition_on(
            conn,
            {
                "name": contest["name"],
                "official_reg_deadline": contest["reg_deadline"],
                "official_time": contest["contest_time"],
                "official_rules": contest["raw_note"],
                "summary": contest["raw_note"],
                "official_site": contest["source_link"],
            },
            status="draft",
            operator=operator,
        )
        if contest["source_link"]:
            add_competition_source(comp_id, contest["source_link"], conn=conn)
        conn.execute(
            "UPDATE contests SET group_id = ? WHERE id = ?",
            (group_id, contest_id),
        )
        _admin.add_audit(operator, group_id, "migrate", "contest", str(contest_id),
                  "", f"->competition {comp_id}", conn=conn)
        return comp_id


_PENDING_UPDATE_COLS = (
    "id, competition_id, fields_json, source_id, change_note, created_by,"
    " created_at, updated_at"
)


def _row_to_pending_update(r: sqlite3.Row) -> dict:
    try:
        fields = json.loads(r[2] or "{}")
    except (ValueError, TypeError):
        fields = {}
    return {
        "id": r[0], "competition_id": r[1], "fields": fields,
        "fields_json": r[2], "source_id": r[3], "change_note": r[4],
        "created_by": r[5], "created_at": r[6], "updated_at": r[7],
    }


def add_pending_update(competition_id: int, fields: dict, *,
                       source_id: int = 0, change_note: str = "",
                       created_by: str = "") -> int:
    """为已确认比赛登记一条待审更新（同一比赛仅保留最新一条，覆盖旧待审）。

    返回待审更新 id（新插入或覆盖旧记录均返回当前记录 id）；校验失败抛 ValueError。
    """
    fields = _contests._clean_competition_patch(fields, ignore_empty=True)
    if not fields:
        raise ValueError("待审更新没有有效字段")
    with _conn._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        source_url = _validate_pending_source_on(conn, competition_id, source_id)
        if source_url and fields.get("official_site", source_url) != source_url:
            raise ValueError("待审官网与抓取来源不一致")
        return _add_pending_update_on(conn, competition_id, fields, source_id, change_note, created_by)


def _add_pending_update_on(conn: sqlite3.Connection, cid: int, fields: dict,
                           source_id: int, note: str, operator: str) -> int:
    row = conn.execute("SELECT id FROM competition_pending_updates WHERE competition_id = ?", (cid,)).fetchone()
    fields_json = json.dumps(fields, ensure_ascii=False)
    if row is not None:
        conn.execute(
            "UPDATE competition_pending_updates SET fields_json = ?, source_id = ?,"
            " change_note = ?, created_by = ?, updated_at = ? WHERE id = ?",
            (fields_json, source_id, note or "", operator or "", _conn.now_str(), row[0]),
        )
        return row[0]
    cur = conn.execute(
        "INSERT INTO competition_pending_updates (competition_id, fields_json,"
        " source_id, change_note, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (cid, fields_json, source_id, note or "", operator or "", _conn.now_str(), _conn.now_str()),
    )
    return cur.lastrowid


def store_crawled_competition(expected_source: dict, patch: dict, new_source_state: dict) -> dict | None:
    """原子核对来源快照并保存待审/旧草稿及基线；过期结果返回 None，零写入。"""
    with _conn._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not source_snapshot_matches(expected_source, conn=conn):
            return None
        cid = expected_source["competition_id"]
        row = conn.execute(f"SELECT {_contests._COMPETITION_COLS} FROM competitions WHERE id = ?", (cid,)).fetchone()
        if row is None or row[11] not in ("draft", "confirmed"):
            return None
        fields = _contests._clean_competition_patch(patch, ignore_empty=True)
        if not fields:
            raise ValueError("来源更新没有有效字段")
        if fields.get("official_site", expected_source["url"]) != expected_source["url"]:
            raise ValueError("更新官网与抓取来源不一致")
        item = {"competition_id": cid}
        if row[11] == "confirmed":
            item["action"] = "pending"
            item["update_id"] = _add_pending_update_on(
                conn, cid, fields, expected_source["id"], "公共来源变化，待超管确认", "crawler:auto",
            )
            status = "pending"
        else:
            sets = ", ".join(f"{field} = ?" for field in fields)
            conn.execute(
                f"UPDATE competitions SET {sets}, updated_at = ? WHERE id = ?",
                [*fields.values(), _conn.now_str(), cid],
            )
            current = conn.execute(f"SELECT {_contests._COMPETITION_COLS} FROM competitions WHERE id = ?", (cid,)).fetchone()
            _contests._add_version(conn, cid, "draft", _contests._row_to_competition(current), "公共来源变化，更新草稿", "crawler:auto")
            item["action"] = "updated"
            status = "draft"
        state = {**new_source_state, "last_status": status, "last_error": ""}
        if not update_source_fetch(expected_source["id"], conn=conn, **state):
            raise ValueError("来源基线保存失败")
        return item


def get_pending_update(competition_id: int) -> dict | None:
    with _conn._connect() as conn:
        row = conn.execute(
            f"SELECT {_PENDING_UPDATE_COLS} FROM competition_pending_updates"
            " WHERE competition_id = ?",
            (competition_id,),
        ).fetchone()
    return _row_to_pending_update(row) if row else None


def list_pending_updates() -> list[dict]:
    """列出全部待审更新（join 比赛名，供 /待审列表 使用）。"""
    with _conn._connect() as conn:
        rows = conn.execute(
            "SELECT pu.id, pu.competition_id, pu.fields_json, pu.source_id,"
            " pu.change_note, pu.created_by, pu.created_at, pu.updated_at,"
            " c.name, c.status"
            " FROM competition_pending_updates pu"
            " JOIN competitions c ON c.id = pu.competition_id"
            " ORDER BY pu.id DESC"
        ).fetchall()
    result = []
    for r in rows:
        item = _row_to_pending_update(r)
        item["name"] = r[8]
        item["competition_status"] = r[9]
        result.append(item)
    return result


def _validate_pending_source_on(conn: sqlite3.Connection, cid: int, source_id: int) -> str | None:
    if source_id:
        row = conn.execute(
            "SELECT url FROM competition_sources WHERE id = ? AND competition_id = ? AND active = 1",
            (source_id, cid),
        ).fetchone()
        if row is None:
            raise ValueError("待审来源已停用或不属于该比赛")
        return row[0]
    return None


def apply_pending_update(update_id: int, operator: str = "") -> bool:
    """批准有效字段的部分更新，保留未包含或待定字段，并记录完整版本。

    与取数、写版本、删待审在同一连接同一事务内完成。
    """
    with _conn._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"SELECT {_PENDING_UPDATE_COLS} FROM competition_pending_updates WHERE id = ?",
            (update_id,),
        ).fetchone()
        if row is None:
            return False
        pending = _row_to_pending_update(row)
        comp_id = pending["competition_id"]
        comp = conn.execute(
            f"SELECT {_contests._COMPETITION_COLS} FROM competitions WHERE id = ?", (comp_id,)
        ).fetchone()
        if comp is None or comp[11] != "confirmed":
            return False
        source_url = _validate_pending_source_on(conn, comp_id, pending["source_id"])
        try:
            fields = json.loads(pending["fields_json"])
        except (ValueError, TypeError):
            raise ValueError("待审内容格式错误") from None
        cleaned = _contests._clean_competition_patch(fields, ignore_empty=True)
        if not cleaned:
            raise ValueError("待审更新没有有效字段")
        if source_url and cleaned.get("official_site", source_url) != source_url:
            raise ValueError("待审官网与抓取来源不一致")
        sets = ", ".join(f"{c} = ?" for c in cleaned)
        args = [cleaned[c] for c in cleaned] + [_conn.now_str(), _conn.now_str(), comp_id]
        conn.execute(
            f"UPDATE competitions SET {sets}, status = 'confirmed',"
            " verified_at = ?, updated_at = ? WHERE id = ?",
            args,
        )
        if "official_site" in cleaned:
            _sync_competition_source_on(conn, comp_id, cleaned["official_site"])
        conn.execute(
            "DELETE FROM competition_pending_updates WHERE id = ?", (update_id,)
        )
        cur_comp = conn.execute(
            f"SELECT {_contests._COMPETITION_COLS} FROM competitions WHERE id = ?", (comp_id,)
        ).fetchone()
        _contests._add_version(conn, comp_id, "confirmed", _contests._row_to_competition(cur_comp),
                     pending["change_note"] or "待审更新通过", operator)
        _admin.add_audit(operator, "", "apply_pending_update", "competition",
                  str(comp_id), "", pending["change_note"] or "", conn=conn)
        if "official_time" in cleaned:
            scoped = {r[0] for r in conn.execute(
                "SELECT group_id FROM competition_notify_groups WHERE competition_id = ?", (comp_id,)
            ).fetchall()}
            _training._sync_contest_start_reminders(conn, comp_id, scoped or None)
        return True


def reject_pending_update(update_id: int, operator: str = "", note: str = "") -> bool:
    """审核拒绝：删除待审更新，正式内容保持不变。"""
    with _conn._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"SELECT {_PENDING_UPDATE_COLS} FROM competition_pending_updates WHERE id = ?",
            (update_id,),
        ).fetchone()
        if row is None:
            return False
        pending = _row_to_pending_update(row)
        conn.execute(
            "DELETE FROM competition_pending_updates WHERE id = ?", (update_id,)
        )
        _admin.add_audit(operator, "", "reject_pending_update", "competition",
                  str(pending["competition_id"]), "",
                  f"拒绝更新：{note or '未说明原因'}", conn=conn)
        return True
