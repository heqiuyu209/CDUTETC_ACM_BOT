"""数据库模块：分层数据模型、按群隔离、草稿审核、活动登记、通用提醒与审计。

数据分层（对应 PRODUCT_SPEC 第 3 节）：
- 公共层（无 group_id，通过关联表授予群访问）：
    competitions / competition_sources / competition_versions
- 学校层：org_events（可标注 shareable 供订阅群共享）
- 群层（每条都带 group_id）：contests（阶段一群级比赛兼容表）、
    training_sessions、group_faq、unanswered_questions、
    collections / collection_responses（征集登记，替代 activities）
- 提醒/审计：reminders（阶段一比赛提醒去重，兼容）、reminder_deliveries
    （通用对象提醒投递：attempts/status/去重）、audit_log

时间约定：所有时间字段均表示北京时间（Asia/Shanghai），格式统一 YYYY-MM-DD HH:MM。
旧版数据兼容：v1 contests 表无 group_id 列时自动补列，旧记录 group_id=''
（未归属任何群），默认不暴露、不参与提醒，由管理员显式迁移归属。
安全约定：
- 所有群级查询在 SQL 层强制带 group_id 过滤，不依赖模型提示词。
- 字段类型严格校验：非字符串直接报错，不做静默转换；时间字段非法报错。
- 写操作走事务；事务提交成功才返回成功。
"""

import hashlib
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

from config import DB_PATH

TZ = ZoneInfo("Asia/Shanghai")
logger = logging.getLogger(__name__)

_MAIN_DDL = """
CREATE TABLE IF NOT EXISTS contests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id      TEXT NOT NULL DEFAULT '',
    name          TEXT NOT NULL DEFAULT '',
    reg_deadline  TEXT NOT NULL DEFAULT '',
    contest_time  TEXT NOT NULL DEFAULT '',
    mode          TEXT NOT NULL DEFAULT '',
    location      TEXT NOT NULL DEFAULT '',
    source_link   TEXT NOT NULL DEFAULT '',
    raw_note      TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT ''
)
"""

_REMIND_DDL = """
CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    TEXT NOT NULL,
    contest_id  INTEGER NOT NULL,
    remind_type TEXT NOT NULL,
    target_time TEXT NOT NULL,
    sent_at     TEXT NOT NULL,
    UNIQUE (group_id, contest_id, remind_type, target_time)
)
"""

# ---------------- 阶段二：公共比赛/学校安排/群订阅 ----------------

_COMPETITIONS_DDL = """
CREATE TABLE IF NOT EXISTS competitions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    edition             TEXT NOT NULL DEFAULT '',
    official_site       TEXT NOT NULL DEFAULT '',
    official_reg_start  TEXT NOT NULL DEFAULT '',
    official_reg_deadline TEXT NOT NULL DEFAULT '',
    official_time       TEXT NOT NULL DEFAULT '',
    tracks              TEXT NOT NULL DEFAULT '',
    official_rules      TEXT NOT NULL DEFAULT '',
    device_req          TEXT NOT NULL DEFAULT '',
    summary             TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'draft',
    verified_at         TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL DEFAULT '',
    updated_at          TEXT NOT NULL DEFAULT ''
)
"""

_WEEKLY_CONTEST_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS weekly_contest_runs (
    week_start      TEXT PRIMARY KEY,
    competition_id  INTEGER NOT NULL,
    created_at      TEXT NOT NULL DEFAULT ''
)
"""

_PUBLICATION_CLAIMS_DDL = """
CREATE TABLE IF NOT EXISTS publication_claims (
    identity_key    TEXT PRIMARY KEY,
    competition_id  INTEGER NOT NULL,
    created_at      TEXT NOT NULL DEFAULT ''
)
"""

# 待超管确认的发布申请：老师发布 → 私聊超管 → 同意入库 / 拒绝丢弃。
# 存整理好的完整字段与原始来源，超管确认前不写 competitions，群内看不到。
_PENDING_PUBLICATIONS_DDL = """
CREATE TABLE IF NOT EXISTS pending_publications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id        TEXT NOT NULL DEFAULT '',
    name            TEXT NOT NULL DEFAULT '',
    edition         TEXT NOT NULL DEFAULT '',
    official_time   TEXT NOT NULL DEFAULT '',
    fields_json     TEXT NOT NULL DEFAULT '{}',
    source_url      TEXT NOT NULL DEFAULT '',
    source_text     TEXT NOT NULL DEFAULT '',
    submitted_by    TEXT NOT NULL DEFAULT '',
    submitted_at    TEXT NOT NULL DEFAULT '',
    duplicate_id    INTEGER NOT NULL DEFAULT 0,
    duplicate_note  TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',
    decided_by      TEXT NOT NULL DEFAULT '',
    decided_at      TEXT NOT NULL DEFAULT ''
)
"""

_COMPETITION_SOURCES_DDL = """
CREATE TABLE IF NOT EXISTS competition_sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    competition_id  INTEGER NOT NULL,
    url             TEXT NOT NULL,
    etag            TEXT NOT NULL DEFAULT '',
    last_modified   TEXT NOT NULL DEFAULT '',
    body_hash       TEXT NOT NULL DEFAULT '',
    last_fetched_at TEXT NOT NULL DEFAULT '',
    last_status     TEXT NOT NULL DEFAULT '',
    last_error      TEXT NOT NULL DEFAULT '',
    active          INTEGER NOT NULL DEFAULT 1,
    revision        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT '',
    UNIQUE (competition_id, url)
)
"""

_COMPETITION_VERSIONS_DDL = """
CREATE TABLE IF NOT EXISTS competition_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    competition_id  INTEGER NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'draft',
    fields_json     TEXT NOT NULL DEFAULT '',
    change_note     TEXT NOT NULL DEFAULT '',
    created_by      TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT ''
)
"""

_PENDING_UPDATES_DDL = """
CREATE TABLE IF NOT EXISTS competition_pending_updates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    competition_id  INTEGER NOT NULL UNIQUE,
    fields_json     TEXT NOT NULL DEFAULT '{}',
    source_id       INTEGER NOT NULL DEFAULT 0,
    change_note     TEXT NOT NULL DEFAULT '',
    created_by      TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL DEFAULT ''
)
"""

_ORG_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS org_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT NOT NULL,
    event_type       TEXT NOT NULL DEFAULT '',
    reg_deadline     TEXT NOT NULL DEFAULT '',
    contest_time     TEXT NOT NULL DEFAULT '',
    location         TEXT NOT NULL DEFAULT '',
    teacher          TEXT NOT NULL DEFAULT '',
    note             TEXT NOT NULL DEFAULT '',
    source           TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'draft',
    shareable        INTEGER NOT NULL DEFAULT 0,
    effective_start  TEXT NOT NULL DEFAULT '',
    effective_end    TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL DEFAULT '',
    updated_at       TEXT NOT NULL DEFAULT ''
)
"""

_TRAINING_SESSIONS_DDL = """
CREATE TABLE IF NOT EXISTS training_sessions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id         TEXT NOT NULL,
    title            TEXT NOT NULL DEFAULT '',
    weekday          TEXT NOT NULL DEFAULT '',
    start_time       TEXT NOT NULL DEFAULT '',
    end_time         TEXT NOT NULL DEFAULT '',
    location         TEXT NOT NULL DEFAULT '',
    needs_laptop     INTEGER NOT NULL DEFAULT 0,
    software         TEXT NOT NULL DEFAULT '',
    device_req       TEXT NOT NULL DEFAULT '',
    training_group   TEXT NOT NULL DEFAULT '',
    effective_start  TEXT NOT NULL DEFAULT '',
    effective_end    TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'confirmed',
    source           TEXT NOT NULL DEFAULT '',
    maintainer       TEXT NOT NULL DEFAULT '',
    updated_at       TEXT NOT NULL DEFAULT ''
)
"""

_GROUP_FAQ_DDL = """
CREATE TABLE IF NOT EXISTS group_faq (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id   TEXT NOT NULL,
    question   TEXT NOT NULL,
    answer     TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'confirmed',
    updated_at TEXT NOT NULL DEFAULT ''
)
"""

_UNANSWERED_DDL = """
CREATE TABLE IF NOT EXISTS unanswered_questions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id  TEXT NOT NULL,
    question  TEXT NOT NULL,
    asker     TEXT NOT NULL DEFAULT '',
    asked_at  TEXT NOT NULL DEFAULT '',
    status    TEXT NOT NULL DEFAULT 'open',
    faq_id    INTEGER NOT NULL DEFAULT 0
)
"""

# ---------------- v2：征集登记（collections 替代 activities） ----------------

_COLLECTIONS_DDL = """
CREATE TABLE IF NOT EXISTS collections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    TEXT NOT NULL,
    title       TEXT NOT NULL,
    purpose     TEXT NOT NULL DEFAULT '',
    deadline    TEXT NOT NULL DEFAULT '',
    capacity    INTEGER,
    created_by  TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'open',
    fields_json TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL DEFAULT ''
)
"""

_COLLECTION_RESPONSES_DDL = """
CREATE TABLE IF NOT EXISTS collection_responses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_id INTEGER NOT NULL,
    group_id      TEXT NOT NULL,
    member_id     TEXT NOT NULL,
    data_json     TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'registered',
    created_at    TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL DEFAULT '',
    UNIQUE (collection_id, member_id)
)
"""

_REMINDER_DELIVERIES_DDL = """
CREATE TABLE IF NOT EXISTS reminder_deliveries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    TEXT NOT NULL,
    obj_type    TEXT NOT NULL,
    obj_id      INTEGER NOT NULL,
    remind_type TEXT NOT NULL,
    target_time TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'pending',
    sent_at     TEXT NOT NULL DEFAULT '',
    last_error  TEXT NOT NULL DEFAULT '',
    dedup_key   TEXT NOT NULL DEFAULT '',
    lease_token TEXT NOT NULL DEFAULT '',
    lease_until TEXT NOT NULL DEFAULT '',
    UNIQUE (dedup_key)
)
"""

_REMINDER_SUCCESS_RECEIPTS_DDL = """
CREATE TABLE IF NOT EXISTS reminder_success_receipts (
    dedup_key   TEXT PRIMARY KEY,
    lease_token TEXT NOT NULL,
    sent_at     TEXT NOT NULL
)
"""

_AUDIT_LOG_DDL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    operator     TEXT NOT NULL DEFAULT '',
    group_id     TEXT NOT NULL DEFAULT '',
    action       TEXT NOT NULL DEFAULT '',
    object_type  TEXT NOT NULL DEFAULT '',
    object_id    TEXT NOT NULL DEFAULT '',
    before_json  TEXT NOT NULL DEFAULT '',
    after_json   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT ''
)
"""

_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_contests_group ON contests(group_id)",
    "CREATE INDEX IF NOT EXISTS idx_reminders_group ON reminders(group_id)",
    "CREATE INDEX IF NOT EXISTS idx_reminders_key ON reminders(group_id, remind_type, target_time)",
    "CREATE INDEX IF NOT EXISTS idx_sources_competition ON competition_sources(competition_id)",
    "CREATE INDEX IF NOT EXISTS idx_sources_url ON competition_sources(url)",
    "CREATE INDEX IF NOT EXISTS idx_training_group ON training_sessions(group_id)",
    "CREATE INDEX IF NOT EXISTS idx_faq_group ON group_faq(group_id)",
    "CREATE INDEX IF NOT EXISTS idx_unanswered_group ON unanswered_questions(group_id)",
    "CREATE INDEX IF NOT EXISTS idx_collections_group ON collections(group_id)",
    "CREATE INDEX IF NOT EXISTS idx_collection_responses_coll ON collection_responses(collection_id)",
    "CREATE INDEX IF NOT EXISTS idx_collection_responses_member ON collection_responses(member_id)",
    "CREATE INDEX IF NOT EXISTS idx_deliveries_status ON reminder_deliveries(status)",
    "CREATE INDEX IF NOT EXISTS idx_audit_group ON audit_log(group_id)",
)

_ALL_DDL = (
    _MAIN_DDL, _REMIND_DDL,
    _COMPETITIONS_DDL, _WEEKLY_CONTEST_RUNS_DDL, _PUBLICATION_CLAIMS_DDL,
    _PENDING_PUBLICATIONS_DDL,
    _COMPETITION_SOURCES_DDL, _COMPETITION_VERSIONS_DDL,
    _PENDING_UPDATES_DDL,
    _ORG_EVENTS_DDL,
    _TRAINING_SESSIONS_DDL, _GROUP_FAQ_DDL, _UNANSWERED_DDL,
    _COLLECTIONS_DDL, _COLLECTION_RESPONSES_DDL,
    _REMINDER_DELIVERIES_DDL, _REMINDER_SUCCESS_RECEIPTS_DDL, _AUDIT_LOG_DDL,
)


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def _as_str(value) -> str:
    """字段必须是字符串；遇到数字等其他类型直接报错，不做静默转换。"""
    if not isinstance(value, str):
        raise ValueError(f"字段类型错误：应为字符串，实际为 {type(value).__name__}")
    return value.strip()


def _as_int(value) -> int | None:
    """容量等整数字段：None 表示不限；非 None 必须是整数。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"字段类型错误：应为整数，实际为 {type(value).__name__}")
    return value


def _clean_time(value) -> str:
    """时间字段：空字符串表示待定；非空必须符合 YYYY-MM-DD HH:MM。"""
    if value is None:
        return ""
    s = _as_str(value)
    if not s:
        return ""
    try:
        parsed = datetime.strptime(s, "%Y-%m-%d %H:%M")
    except ValueError:
        raise ValueError(f"时间格式非法（应为 YYYY-MM-DD HH:MM）: {s}") from None
    return parsed.strftime("%Y-%m-%d %H:%M")


def _canonical_time(text: str) -> str:
    """同一北京时间使用同一存储身份；旧秒级时间保留非零秒，非法旧值原样保留。"""
    parsed = _delivery_parse_time(text)
    if parsed is None:
        return text
    fmt = "%Y-%m-%d %H:%M:%S" if parsed.second else "%Y-%m-%d %H:%M"
    return parsed.strftime(fmt)


def _migrate_business_times(conn: sqlite3.Connection) -> None:
    """只规范化运行中的业务日期，不改审计和历史版本中的原始资料。"""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    columns = {
        "contests": ("reg_deadline", "contest_time"),
        "competitions": ("official_reg_start", "official_reg_deadline", "official_time"),
        "collections": ("deadline",),
        "org_events": ("reg_deadline", "contest_time", "effective_start", "effective_end"),
        "training_sessions": ("effective_start", "effective_end"),
    }
    for table, fields in columns.items():
        for row in conn.execute(f"SELECT id, {', '.join(fields)} FROM {table}").fetchall():
            changed = {}
            for field, value in zip(fields, row[1:]):
                canonical = _canonical_time(value)
                if canonical != value:
                    changed[field] = canonical
            if changed:
                sets = ", ".join(f"{field} = ?" for field in changed)
                conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", [*changed.values(), row[0]])

    # 旧版成功标记也按真实时刻合并，避免仅改变补零格式后再发一次。
    grouped: dict[tuple, list[tuple]] = {}
    for row in conn.execute(
        "SELECT id, group_id, contest_id, remind_type, target_time, sent_at FROM reminders ORDER BY id"
    ).fetchall():
        identity = (*row[1:4], _canonical_time(row[4]))
        grouped.setdefault(identity, []).append(row)
    for identity, rows in grouped.items():
        winner = min(rows, key=lambda row: (row[5], row[0]))
        for row in rows:
            if row[0] != winner[0]:
                conn.execute("DELETE FROM reminders WHERE id = ?", (row[0],))
        if winner[4] != identity[3]:
            conn.execute("UPDATE reminders SET target_time = ? WHERE id = ?", (identity[3], winner[0]))


def now_str() -> str:
    """当前北京时间（Asia/Shanghai），格式 YYYY-MM-DD HH:MM:SS。"""
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def _migrate_v1(conn: sqlite3.Connection) -> None:
    """旧版表结构迁移：为 contests 补充 group_id 列（旧记录置空，隔离保留）。"""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(contests)").fetchall()]
    if "group_id" not in cols:
        conn.execute("ALTER TABLE contests ADD COLUMN group_id TEXT NOT NULL DEFAULT ''")


def _migrate_source_activity(conn: sqlite3.Connection) -> None:
    """旧库保留来源历史，并使此前修正过的官网成为有效监控地址。"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(competition_sources)")}
    if "active" in cols and "revision" in cols:
        return
    conn.execute("SAVEPOINT source_activity_migration")
    try:
        if "revision" not in cols:
            conn.execute("ALTER TABLE competition_sources ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
        if "active" not in cols:
            conn.execute("ALTER TABLE competition_sources ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
            for cid, url in conn.execute("SELECT id, official_site FROM competitions WHERE official_site != ''").fetchall():
                if url.strip():
                    _sync_competition_source_on(conn, cid, url.strip())
    except Exception:
        conn.execute("ROLLBACK TO source_activity_migration")
        conn.execute("RELEASE source_activity_migration")
        raise
    conn.execute("RELEASE source_activity_migration")


def init_db() -> None:
    """初始化数据库：建表 + 旧库迁移 + 索引。程序启动时调用一次。"""
    with _connect() as conn:
        # 查列与 ALTER 必须位于同一写事务，避免两个启动进程同时补列。
        # 调用方已有事务时保留它；普通新连接先取得数据库写锁再读 schema。
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        for ddl in _ALL_DDL:
            conn.execute(ddl)
        _migrate_v1(conn)
        _migrate_business_times(conn)
        _migrate_reminder_deliveries(conn)
        for ddl in _INDEX_DDL:
            conn.execute(ddl)
        _migrate_source_activity(conn)


# =====================================================================
# 阶段一兼容：群级比赛（contests 表）
# =====================================================================


def save_contest(group_id: str, info: dict) -> int:
    """写入一条群级比赛记录，返回新记录 id。字段必须是字符串，否则抛 ValueError。"""
    values = (
        group_id.strip(),
        _as_str(info.get("name")),
        _canonical_time(_as_str(info.get("reg_deadline") or "")),
        _canonical_time(_as_str(info.get("contest_time") or "")),
        _as_str(info.get("mode") or ""),
        _as_str(info.get("location") or ""),
        _as_str(info.get("source_link") or ""),
        _as_str(info.get("raw_note") or ""),
        now_str(),
    )
    with _connect() as conn:
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


def list_contests(group_id: str | None = None) -> list[dict]:
    """查询群级比赛记录。

    - 传入 group_id：只返回该群记录（旧记录 group_id 为空，不会混入任何具体群）。
    - 不传：返回全部记录（含未归属旧记录），仅用于管理/迁移场景。
    """
    with _connect() as conn:
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


def list_recent_contests(group_id: str, limit: int = 5) -> list[dict]:
    """查询某群最近 N 条旧版群级比赛，保留兼容接口。"""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_CONTEST_COLS} FROM contests WHERE group_id = ? ORDER BY id DESC LIMIT ?",
            (group_id, limit),
        ).fetchall()
    return [_row_to_contest(r) for r in rows]


def list_question_contests(group_id: str, limit: int = 5) -> list[dict]:
    """查询问答用比赛：已确认公共比赛 + 本群已归属的旧记录。

    调用方须先校验群白名单。v2 公共正式比赛供允许群共享，草稿与待审
    更新不进入上下文；旧记录在 SQL 层按群过滤。按最近更新/创建时间
    排序并限量，统一为问答使用的名称、时间、形式、地点和来源字段。
    """
    if not group_id or limit <= 0:
        return []
    fields = ("name", "reg_deadline", "contest_time", "mode", "location", "source_link")
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT name, reg_deadline, contest_time, mode, location, source_link
            FROM (
                SELECT name, official_reg_deadline AS reg_deadline,
                       official_time AS contest_time, '' AS mode, '' AS location,
                       official_site AS source_link, updated_at AS sort_time,
                       1 AS is_public, id
                FROM competitions WHERE status = 'confirmed'
                UNION ALL
                SELECT name, reg_deadline, contest_time, mode, location,
                       source_link, created_at AS sort_time, 0 AS is_public, id
                FROM contests WHERE group_id = ?
            )
            ORDER BY sort_time DESC, is_public DESC, id DESC
            LIMIT ?
            """,
            (group_id, limit),
        ).fetchall()
    return [dict(zip(fields, row)) for row in rows]


def delete_contest(group_id: str, cid: int) -> bool:
    """按 id 删除某群的群级比赛（仅限本群记录）。"""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM contests WHERE id = ? AND group_id = ?", (cid, group_id)
        )
        return cur.rowcount > 0


# ---------- 提醒去重（阶段一兼容） ----------


def has_reminded(group_id: str, contest_id: int, remind_type: str, target_time: str) -> bool:
    """是否已对该（比赛, 类型, 目标时间）发过提醒。"""
    target_time = _canonical_time(target_time)
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM reminders WHERE group_id=? AND contest_id=? AND remind_type=? AND target_time=?",
            (group_id, contest_id, remind_type, target_time),
        ).fetchone()
        return row is not None


def mark_reminded(group_id: str, contest_id: int, remind_type: str, target_time: str) -> bool:
    """标记已提醒（发送成功后才可调用）。"""
    target_time = _canonical_time(target_time)
    with _connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO reminders (group_id, contest_id, remind_type, target_time, sent_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                group_id, contest_id, remind_type, target_time, now_str(),
            ),
        )
        return cur.rowcount > 0


# =====================================================================
# 公共比赛（competitions）与来源 / 版本
# =====================================================================

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
    name = _as_str(info.get("name"))
    if not name:
        raise ValueError("比赛名称不能为空")
    cleaned = {"name": name}
    for field in (
        "edition", "official_site", "tracks", "official_rules",
        "device_req", "summary",
    ):
        cleaned[field] = _as_str(info.get(field) or "")
    for field in ("official_reg_start", "official_reg_deadline", "official_time"):
        cleaned[field] = _clean_time(info.get(field))
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
            cleaned[field] = _as_str(value)
            if not cleaned[field]:
                raise ValueError("比赛名称不能为空")
        elif field in ("official_reg_start", "official_reg_deadline", "official_time"):
            cleaned[field] = _clean_time(value)
        else:
            cleaned[field] = _as_str(value if value is not None else "")
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


def _create_competition_on(conn: sqlite3.Connection, info: dict,
                           status: str, operator: str) -> int:
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
            now_str() if status == "confirmed" else "",
            now_str(), now_str(),
        ),
    )
    cid = cur.lastrowid
    _add_version(conn, cid, "draft" if status == "draft" else "confirmed",
                 cleaned, "创建", operator)
    return cid


def create_competition(info: dict, status: str = "draft", operator: str = "",
                       conn: sqlite3.Connection | None = None) -> int:
    """创建公共比赛（默认草稿）。校验失败抛 ValueError，不写库。"""
    if conn is not None:
        return _create_competition_on(conn, info, status, operator)
    with _connect() as conn:
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
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT competition_id FROM weekly_contest_runs WHERE week_start = ?",
            (week_start,),
        ).fetchone()
        if existing is not None:
            return existing[0], False
        cid = _create_competition_on(conn, info, "confirmed", "weekly:auto")
        conn.execute(
            "INSERT INTO weekly_contest_runs (week_start, competition_id, created_at)"
            " VALUES (?, ?, ?)",
            (week_start, cid, now_str()),
        )
    return cid, True


def get_competition(cid: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_COMPETITION_COLS} FROM competitions WHERE id = ?", (cid,)
        ).fetchone()
    return _row_to_competition(row) if row else None


def list_competitions(status: str | None = None) -> list[dict]:
    """列出公共比赛；status 可选过滤（draft/confirmed）。"""
    with _connect() as conn:
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
        (cid, kind, json.dumps(fields, ensure_ascii=False), note, operator or "", now_str()),
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
            name = _as_str(info[field])
            if not name:
                raise ValueError("比赛名称不能为空")
            cleaned[field] = name
        elif field in ("official_reg_start", "official_reg_deadline", "official_time"):
            cleaned[field] = _clean_time(info[field])
        else:
            cleaned[field] = _as_str(info[field] or "")
    cols = list(cleaned)
    if not cols:
        return False
    sets = ", ".join(f"{c} = ?" for c in cols)
    args = [cleaned[c] for c in cols] + [now_str(), cid]
    with _connect() as conn:
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
        return True


def confirm_competition(cid: int, operator: str = "") -> bool:
    """老师确认草稿：status → confirmed，记录 confirmed 版本与核验时间。"""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE competitions SET status = 'confirmed', verified_at = ?, updated_at = ?"
            " WHERE id = ? AND status != 'confirmed'",
            (now_str(), now_str(), cid),
        )
        if cur.rowcount == 0:
            return False
        row = conn.execute(
            f"SELECT {_COMPETITION_COLS} FROM competitions WHERE id = ?", (cid,)
        ).fetchone()
        _add_version(conn, cid, "confirmed", _row_to_competition(row), "确认发布", operator)
        return True


def reject_competition(cid: int, operator: str = "", note: str = "") -> bool:
    """老师拒绝草稿：保持 draft 并记录拒绝说明（不删除数据）。"""
    with _connect() as conn:
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
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE competitions SET status = ?, verified_at = CASE WHEN ? = 'confirmed'"
            " THEN ? ELSE verified_at END, updated_at = ? WHERE id = ? AND status != ?",
            (status, status, now_str(), now_str(), cid, status),
        )
        if cur.rowcount == 0:
            return False
        row = conn.execute(
            f"SELECT {_COMPETITION_COLS} FROM competitions WHERE id = ?", (cid,)
        ).fetchone()
        _add_version(conn, cid, status, _row_to_competition(row),
                     note or f"状态切换为 {status}", operator)
        return True


def list_competition_versions(cid: int) -> list[dict]:
    with _connect() as conn:
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


# ---------- 公共来源 ----------

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
    url = _as_str(url)
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
        cur = conn.execute(sql_ins, (cid, url, now_str()))
        if cur.rowcount > 0:
            return cur.lastrowid
        return conn.execute(sql_sel, (cid, url)).fetchone()[0]
    with _connect() as conn:
        cur = conn.execute(sql_ins, (cid, url, now_str()))
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
    with _connect() as conn:
        return conn.execute(query, values).fetchone() is not None


def update_source_fetch(source_id: int, *, etag: str = "", last_modified: str = "",
                        body_hash: str = "", last_status: str = "",
                        last_error: str = "", last_fetched_at: str | None = None,
                        conn: sqlite3.Connection | None = None,
                        expected_source: dict | None = None) -> bool:
    """条件更新有效来源；expected_source 过期时零写入，返回 False。"""
    if last_fetched_at is None:
        last_fetched_at = now_str()
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
    with _connect() as conn:
        return conn.execute(sql, values).rowcount == 1


def get_source_by_url(url: str, competition_id: int | None = None) -> dict | None:
    """查询有效来源；指定比赛时不借用其他比赛相同 URL 的基线。"""
    where = "url = ? AND active = 1"
    args = [url]
    if competition_id is not None:
        where += " AND competition_id = ?"
        args.append(competition_id)
    with _connect() as conn:
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
    with _connect() as conn:
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
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_COMPETITION_COLS} FROM competitions"
            " WHERE status IN ('confirmed', 'draft')"
        ).fetchall()
    for row in rows:
        record = _row_to_competition(row)
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
    for row in conn.execute(f"SELECT {_COMPETITION_COLS} FROM competitions ORDER BY id"):
        competition = _row_to_competition(row)
        cid = competition["id"]
        if cid in claimed_ids:
            # 已有持久身份的记录不再从可编辑字段推导额外身份。
            continue
        candidates = list(original_versions.get(cid, []))
        if competition["status"] == "confirmed":
            candidates.append(competition)
        for candidate in candidates:
            try:
                cleaned = _clean_competition_info(candidate)
                if _publication_identity(cleaned, canonical_url) != identity:
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


def _create_confirmed_from_pending(info: dict, source_url: str, source_text: str,
                                   operator: str) -> tuple[int, bool]:
    """超管确认后落库：无来源链接（群消息转发）时按「名称+届次+时间」去重。

    与 create_published_competition_once 的差别：
    - 不要求 source_url，没有链接也能发布；
    - 去重口径统一为 find_duplicate_competition（名称+届次+比赛时间），
      因此转发消息与官网发布的两条同场比赛仍会被判重。
    """
    if not isinstance(info, dict):
        raise ValueError("比赛信息必须是对象")
    cleaned = _clean_competition_info(info)
    existing = find_duplicate_competition(cleaned)
    if existing is not None:
        return existing["id"], False
    source_url = _as_str(source_url)
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # 事务内二次确认，避免并发同意同一条申请造成重复入库。
        rows = conn.execute(
            f"SELECT {_COMPETITION_COLS} FROM competitions"
            " WHERE status IN ('confirmed', 'draft')"
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
        cid = _create_competition_on(conn, cleaned, "confirmed", operator)
        if source_url:
            sid = add_competition_source(cid, source_url, conn=conn)
            body_hash = hashlib.sha256((source_text or "").encode("utf-8")).hexdigest()
            update_source_fetch(
                sid, body_hash=body_hash, last_status="confirmed", conn=conn,
            )
        return cid, True


def create_published_competition_once(info: dict, source_url: str, source_text: str,
                                     operator: str = "") -> tuple[int, bool]:
    """原子发布，返回（比赛 id，是否新建）；重复不改资料或成功正文基线。

    持久发布身份独立于后续手工修正；写事务内完成查重、来源和版本创建。
    """
    source_url = _as_str(source_url)
    if not source_url:
        raise ValueError("来源 URL 不能为空")
    if not isinstance(source_text, str):
        raise ValueError("来源正文必须是字符串")
    if not isinstance(info, dict):
        raise ValueError("比赛信息必须是对象")
    cleaned = _clean_competition_info({**info, "official_site": source_url})
    canonical_url = _canonical_publication_url(source_url)
    identity = _publication_identity(cleaned, canonical_url)
    body_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT competition_id FROM publication_claims WHERE identity_key = ?",
            (identity,),
        ).fetchone()
        if existing is not None:
            return existing[0], False
        cid = _find_legacy_publication(conn, identity, canonical_url)
        if cid is not None:
            conn.execute(
                "INSERT INTO publication_claims (identity_key, competition_id, created_at)"
                " VALUES (?, ?, ?)", (identity, cid, now_str()),
            )
            return cid, False
        cid = _create_competition_on(conn, cleaned, "confirmed", operator)
        sid = add_competition_source(cid, source_url, conn=conn)
        update_source_fetch(
            sid, body_hash=body_hash, last_status="confirmed", conn=conn,
        )
        conn.execute(
            "INSERT INTO publication_claims (identity_key, competition_id, created_at)"
            " VALUES (?, ?, ?)", (identity, cid, now_str()),
        )
        return cid, True


def create_published_competition(info: dict, source_url: str, source_text: str,
                                operator: str = "") -> int:
    """兼容原返回值的发布入口，重复发布仍返回同一个比赛 id。"""
    cid, _ = create_published_competition_once(info, source_url, source_text, operator)
    return cid


# =====================================================================
# 待超管确认的发布申请（pending_publications）
# =====================================================================

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
        return "" if value is None else _as_str(value)

    duplicate_id = int(duplicate["id"]) if duplicate else 0
    duplicate_note = ""
    if duplicate:
        duplicate_note = (
            f"已存在同名同届次同时间的记录（编号 {duplicate_id}，"
            f"状态 {duplicate.get('status') or '未知'}）"
        )
    with _connect() as conn:
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
                now_str(),
                duplicate_id,
                duplicate_note,
            ),
        )
        return cur.lastrowid


def get_pending_publication(pid: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_PENDING_PUB_COLS} FROM pending_publications WHERE id = ?", (pid,)
        ).fetchone()
    return _row_to_pending_publication(row) if row else None


def list_pending_publications(status: str = "pending") -> list[dict]:
    """列出发布申请；status 传空串则返回全部。"""
    with _connect() as conn:
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
    """超管同意：原子地把申请转成正式比赛并标记已通过。

    Returns:
        (competition_id, created)：created=False 表示期间已存在相同记录，未重复创建。
        申请不存在或非 pending 状态时抛 ValueError。
    """
    pending = get_pending_publication(pid)
    if pending is None:
        raise ValueError(f"发布申请 #{pid} 不存在")
    if pending["status"] != "pending":
        raise ValueError(f"发布申请 #{pid} 已是 {pending['status']} 状态")
    info = json.loads(pending["fields_json"] or "{}")
    cid, created = _create_confirmed_from_pending(
        info,
        source_url=pending["source_url"],
        source_text=pending["source_text"],
        operator=f"approve:{operator or pending['submitted_by']}",
    )
    with _connect() as conn:
        conn.execute(
            "UPDATE pending_publications SET status = 'approved', decided_by = ?,"
            " decided_at = ? WHERE id = ?", (_as_str(operator), now_str(), pid),
        )
        add_audit(operator, pending["group_id"], "approve_publication",
                  "pending_publication", str(pid), "", f"competition:{cid}", conn=conn)
    return cid, created


def reject_pending_publication(pid: int, operator: str = "", note: str = "") -> bool:
    """超管拒绝：标记已拒绝，不写任何比赛数据。"""
    pending = get_pending_publication(pid)
    if pending is None or pending["status"] != "pending":
        return False
    with _connect() as conn:
        conn.execute(
            "UPDATE pending_publications SET status = 'rejected', decided_by = ?,"
            " decided_at = ? WHERE id = ?", (_as_str(operator), now_str(), pid),
        )
        add_audit(operator, pending["group_id"], "reject_publication",
                  "pending_publication", str(pid), "", note or "超管拒绝", conn=conn)
    return True


# =====================================================================
# 学校/集训队统一安排（org_events）
# =====================================================================

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
    title = _as_str(info.get("title"))
    if not title:
        raise ValueError("安排标题不能为空")
    values = (
        title,
        _as_str(info.get("event_type") or ""),
        _clean_time(info.get("reg_deadline")),
        _clean_time(info.get("contest_time")),
        _as_str(info.get("location") or ""),
        _as_str(info.get("teacher") or ""),
        _as_str(info.get("note") or ""),
        _as_str(info.get("source") or ""),
        _as_str(info.get("status") or "draft"),
        1 if info.get("shareable") else 0,
        _clean_time(info.get("effective_start")),
        _clean_time(info.get("effective_end")),
        now_str(), now_str(),
    )
    if values[8] not in ("draft", "confirmed"):
        raise ValueError("状态必须是 draft 或 confirmed")
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO org_events (title, event_type, reg_deadline, contest_time,"
            " location, teacher, note, source, status, shareable, effective_start,"
            " effective_end, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
        add_audit(operator, "", "create", "org_event", str(cur.lastrowid), "",
                  json.dumps(values, ensure_ascii=False), conn=conn)
        return cur.lastrowid


def list_org_events(status: str | None = None, shareable_only: bool = False) -> list[dict]:
    with _connect() as conn:
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
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE org_events SET status = 'confirmed', updated_at = ? WHERE id = ?",
            (now_str(), eid),
        )
        if cur.rowcount:
            add_audit(operator, "", "confirm", "org_event", str(eid), "",
                      "confirmed", conn=conn)
        return cur.rowcount > 0


def update_org_event(eid: int, info: dict, operator: str = "") -> bool:
    """更新学校安排字段（仅传入字段）。"""
    mapping = {
        "title": lambda v: (_as_str(v),),
        "event_type": lambda v: (_as_str(v or ""),),
        "reg_deadline": lambda v: (_clean_time(v),),
        "contest_time": lambda v: (_clean_time(v),),
        "location": lambda v: (_as_str(v or ""),),
        "teacher": lambda v: (_as_str(v or ""),),
        "note": lambda v: (_as_str(v or ""),),
        "source": lambda v: (_as_str(v or ""),),
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
    args += [now_str(), eid]
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE org_events SET {', '.join(sets)}, updated_at = ? WHERE id = ?", args
        )
        if cur.rowcount:
            add_audit(operator, "", "update", "org_event", str(eid), "",
                      json.dumps(info, ensure_ascii=False), conn=conn)
        return cur.rowcount > 0


# =====================================================================
# 群内课程安排（training_sessions）
# =====================================================================

_TRAINING_COLS = (
    "id, group_id, title, weekday, start_time, end_time, location, needs_laptop,"
    " software, device_req, training_group, effective_start, effective_end,"
    " status, source, maintainer, updated_at"
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


def training_occurrence_times(session: dict, now: datetime, before_hours: int) -> list[datetime]:
    """展开提醒窗口内尚未开始的每周课程，未知星期/时间不产生猜测计划。"""
    now = _delivery_now(now)
    weekday = _training_weekday(session.get("weekday"))
    clock = _training_start_clock(session.get("start_time"))
    if weekday is None or clock is None or before_hours <= 0:
        return []
    day = now.date() + timedelta(days=(weekday - now.weekday()) % 7)
    target = datetime.combine(day, clock, tzinfo=TZ)
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
    }


def create_training_session(group_id: str, info: dict, operator: str = "") -> int:
    """创建群内课程/训练安排。group_id 必填。"""
    group_id = _as_str(group_id)
    title = _as_str(info.get("title") or "")
    if not title:
        title = f"训练安排 {now_str()[:10]}"
    values = (
        group_id, title,
        _as_str(info.get("weekday") or ""),
        _as_str(info.get("start_time") or ""),
        _as_str(info.get("end_time") or ""),
        _as_str(info.get("location") or ""),
        1 if info.get("needs_laptop") else 0,
        _as_str(info.get("software") or ""),
        _as_str(info.get("device_req") or ""),
        _as_str(info.get("training_group") or ""),
        _clean_time(info.get("effective_start")),
        _clean_time(info.get("effective_end")),
        _as_str(info.get("status") or "confirmed"),
        _as_str(info.get("source") or ""),
        _as_str(operator or ""),
        now_str(),
    )
    if values[12] not in ("confirmed", "draft", "cancelled"):
        raise ValueError("状态必须是 confirmed/draft/cancelled")
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO training_sessions (group_id, title, weekday, start_time,"
            " end_time, location, needs_laptop, software, device_req, training_group,"
            " effective_start, effective_end, status, source, maintainer, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
        return cur.lastrowid


def list_training_sessions(group_id: str, active_only: bool = True,
                           include_cancelled: bool = False) -> list[dict]:
    """列出某群课程安排。active_only：仅生效期内且未取消。"""
    with _connect() as conn:
        where = "group_id = ?"
        args: list = [group_id]
        if active_only:
            now = now_str()
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
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE training_sessions SET status = 'cancelled', updated_at = ?"
            " WHERE id = ? AND group_id = ?",
            (now_str(), tid, group_id),
        )
        if cur.rowcount:
            add_audit(operator, group_id, "cancel", "training_session", str(tid), "",
                      "cancelled", conn=conn)
        return cur.rowcount > 0


# =====================================================================
# 群内 FAQ
# =====================================================================

_FAQ_COLS = "id, group_id, question, answer, source, status, updated_at"


def _row_to_faq(r: sqlite3.Row) -> dict:
    return {
        "id": r[0], "group_id": r[1], "question": r[2], "answer": r[3],
        "source": r[4], "status": r[5], "updated_at": r[6],
    }


def _normalize_question(question: str) -> str:
    """只规范空白；不改变标点、大小写或按关键词猜测同题。"""
    return " ".join(_as_str(question).split())


def add_faq(group_id: str, question: str, answer: str, source: str = "",
            operator: str = "") -> int:
    """添加 FAQ，并在同一事务关闭本群规范空白后完全相同的未答问题。"""
    question = _normalize_question(question)
    answer = _as_str(answer)
    if not question or not answer:
        raise ValueError("FAQ 的问题与答案都不能为空")
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO group_faq (group_id, question, answer, source, status, updated_at)"
            " VALUES (?, ?, ?, ?, 'confirmed', ?)",
            (group_id, question, answer, _as_str(source or ""), now_str()),
        )
        fid = cur.lastrowid
        pending = conn.execute(
            "SELECT id, question FROM unanswered_questions WHERE group_id = ? AND status = 'open'",
            (group_id,),
        ).fetchall()
        for qid, stored_question in pending:
            if _normalize_question(stored_question) == question:
                conn.execute(
                    "UPDATE unanswered_questions SET status = 'answered', faq_id = ?"
                    " WHERE id = ? AND group_id = ? AND status = 'open'",
                    (fid, qid, group_id),
                )
        add_audit(operator, group_id, "create", "faq", str(fid), "", question[:50], conn=conn)
        return fid


def list_faq(group_id: str, keyword: str | None = None) -> list[dict]:
    with _connect() as conn:
        if keyword:
            rows = conn.execute(
                f"SELECT {_FAQ_COLS} FROM group_faq WHERE group_id = ?"
                " AND (question LIKE ? OR answer LIKE ?) AND status != 'deleted'"
                " ORDER BY id DESC",
                (group_id, f"%{keyword}%", f"%{keyword}%"),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_FAQ_COLS} FROM group_faq WHERE group_id = ? AND status != 'deleted'"
                " ORDER BY id DESC",
                (group_id,),
            ).fetchall()
    return [_row_to_faq(r) for r in rows]


def delete_faq(group_id: str, faq_id: int, operator: str = "") -> bool:
    """标记 FAQ 删除（保留审计，不物理删除）。"""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE group_faq SET status = 'deleted', updated_at = ? WHERE id = ? AND group_id = ?",
            (now_str(), faq_id, group_id),
        )
        if cur.rowcount:
            add_audit(operator, group_id, "delete", "faq", str(faq_id), "",
                      "deleted", conn=conn)
        return cur.rowcount > 0


# =====================================================================
# 未答问题收集
# =====================================================================

_UNANSWERED_COLS = "id, group_id, question, asker, asked_at, status, faq_id"


def _row_to_unanswered(r: sqlite3.Row) -> dict:
    return {
        "id": r[0], "group_id": r[1], "question": r[2], "asker": r[3],
        "asked_at": r[4], "status": r[5], "faq_id": r[6],
    }


def add_unanswered_question(group_id: str, question: str, asker: str = "") -> int:
    question = _normalize_question(question)
    if not question:
        raise ValueError("问题不能为空")
    with _connect() as conn:
        # 相同问题合并计数：存在 open 同题则复用（不重复收集）
        row = conn.execute(
            "SELECT id FROM unanswered_questions WHERE group_id = ? AND question = ? AND status = 'open'",
            (group_id, question),
        ).fetchone()
        if row:
            return row[0]
        cur = conn.execute(
            "INSERT INTO unanswered_questions (group_id, question, asker, asked_at, status)"
            " VALUES (?, ?, ?, ?, 'open')",
            (group_id, question, _as_str(asker or ""), now_str()),
        )
        return cur.lastrowid


def list_unanswered(group_id: str, status: str = "open") -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_UNANSWERED_COLS} FROM unanswered_questions"
            " WHERE group_id = ? AND status = ? ORDER BY id DESC",
            (group_id, status),
        ).fetchall()
    return [_row_to_unanswered(r) for r in rows]


def mark_question_answered(qid: int, faq_id: int = 0) -> bool:
    """问题已被回答（合并进 FAQ 后标记 answered）。"""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE unanswered_questions SET status = 'answered', faq_id = ? WHERE id = ?",
            (faq_id, qid),
        )
        return cur.rowcount > 0


# =====================================================================
# v2 征集登记（collections / collection_responses）
# =====================================================================

_COLLECTION_COLS = (
    "id, group_id, title, purpose, deadline, capacity, created_by, status,"
    " fields_json, created_at, updated_at"
)


def _row_to_collection(r: sqlite3.Row) -> dict:
    return {
        "id": r[0], "group_id": r[1], "title": r[2], "purpose": r[3],
        "deadline": r[4], "capacity": r[5], "created_by": r[6], "status": r[7],
        "fields_json": r[8], "created_at": r[9], "updated_at": r[10],
    }


def create_collection(group_id: str, title: str, purpose: str = "", deadline: str = "",
                      capacity: int | None = None, fields: list | None = None,
                      operator: str = "") -> int:
    """发起征集（报名意向/设备借用/出勤等）。capacity=None 表示不限。"""
    group_id = _as_str(group_id)
    title = _as_str(title)
    if not title:
        raise ValueError("征集标题不能为空")
    fields = fields or []
    if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
        raise ValueError("登记字段必须是字符串列表")
    deadline = _clean_time(deadline)
    capacity = _as_int(capacity)
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO collections (group_id, title, purpose, deadline, capacity,"
            " created_by, status, fields_json, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                group_id, title, _as_str(purpose or ""), deadline, capacity,
                _as_str(operator or ""), "open",
                json.dumps(fields, ensure_ascii=False), now_str(), now_str(),
            ),
        )
        add_audit(operator, group_id, "create", "collection", str(cur.lastrowid), "",
                  title, conn=conn)
        return cur.lastrowid


def list_collections(group_id: str, status: str | None = None) -> list[dict]:
    with _connect() as conn:
        if status is None:
            rows = conn.execute(
                f"SELECT {_COLLECTION_COLS} FROM collections WHERE group_id = ? ORDER BY id DESC",
                (group_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_COLLECTION_COLS} FROM collections WHERE group_id = ? AND status = ?"
                " ORDER BY id DESC",
                (group_id, status),
            ).fetchall()
    return [_row_to_collection(r) for r in rows]


def list_collections_all(status: str | None = None) -> list[dict]:
    """列出全部群的征集（供全局提醒扫描用，提醒发送按记录的 group_id 归群）。"""
    with _connect() as conn:
        if status is None:
            rows = conn.execute(
                f"SELECT {_COLLECTION_COLS} FROM collections ORDER BY id"
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_COLLECTION_COLS} FROM collections WHERE status = ? ORDER BY id",
                (status,),
            ).fetchall()
    return [_row_to_collection(r) for r in rows]


def get_collection(cid: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_COLLECTION_COLS} FROM collections WHERE id = ?", (cid,)
        ).fetchone()
    return _row_to_collection(row) if row else None


def set_collection_status(cid: int, status: str, operator: str = "") -> bool:
    """关闭/恢复征集（status ∈ open/closed）。"""
    if status not in ("open", "closed"):
        raise ValueError(f"征集状态非法：{status}")
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE collections SET status = ?, updated_at = ? WHERE id = ?",
            (status, now_str(), cid),
        )
        if cur.rowcount == 0:
            return False
        add_audit(operator, "", "set_status", "collection", str(cid),
                  "", status, conn=conn)
        return True


_COLLECTION_RESPONSE_COLS = (
    "id, collection_id, group_id, member_id, data_json, status, created_at, updated_at"
)


def _row_to_collection_response(r: sqlite3.Row) -> dict:
    return {
        "id": r[0], "collection_id": r[1], "group_id": r[2], "member_id": r[3],
        "data_json": r[4], "status": r[5], "created_at": r[6], "updated_at": r[7],
    }


def register_collection(cid: int, group_id: str, member_id: str,
                        data: dict | None = None) -> tuple[bool, str]:
    """成员报名/修改自己的征集登记（幂等：同一征集+成员只保留一条 registered）。

    校验：
    - 征集存在且属于该群、状态 open（征集期）
    - data 只能包含征集声明的字段（fields_json）
    - 已取消的登记重新登记 = 恢复；已登记的再次登记 = 更新字段（不重复计数）
    """
    if data is not None and not isinstance(data, dict):
        return False, "报名内容格式错误"
    cleaned: dict[str, str] = {}
    for key, value in (data or {}).items():
        if not isinstance(key, str) or not key.strip():
            return False, "报名字段名称应为非空文本"
        if not isinstance(value, str):
            return False, f"字段 {key} 应为文本"
        cleaned[key.strip()] = value.strip()

    with _connect() as conn:
        # 状态、截止、名额和个人登记均在同一写事务中检查，避免并发超额。
        conn.execute("BEGIN IMMEDIATE")
        coll_row = conn.execute(
            f"SELECT {_COLLECTION_COLS} FROM collections WHERE id = ?", (cid,),
        ).fetchone()
        if coll_row is None:
            return False, "征集不存在"
        coll = _row_to_collection(coll_row)
        if coll["group_id"] != group_id:
            return False, "征集不属于本群"
        if coll["status"] != "open":
            return False, "征集已结束或取消，无法报名"
        if coll["deadline"]:
            deadline = _delivery_parse_time(coll["deadline"])
            if deadline is None:
                return False, "征集截止时间无效，请联系老师"
            if deadline <= _delivery_parse_time(now_str()):
                return False, "已超过征集截止时间"
        try:
            declared = json.loads(coll["fields_json"] or "[]")
        except (ValueError, TypeError):
            return False, "征集登记字段配置错误，请联系老师"
        if not isinstance(declared, list) or not all(isinstance(field, str) for field in declared):
            return False, "征集登记字段配置错误，请联系老师"
        for key in cleaned:
            if declared and key not in declared:
                return False, f"该征集不支持字段：{key}"
        row = conn.execute(
            "SELECT id, status, data_json FROM collection_responses WHERE collection_id = ? AND member_id = ?",
            (cid, member_id),
        ).fetchone()
        merged = {}
        if row is not None:
            try:
                merged = json.loads(row[2] or "{}")
            except (ValueError, TypeError):
                return False, "原报名资料格式错误，请联系老师"
            if not isinstance(merged, dict):
                return False, "原报名资料格式错误，请联系老师"
            if row[1] == "registered" and not cleaned:
                return True, "你已报名，原登记资料已保留"
        merged.update(cleaned)
        if row is None or row[1] != "registered":
            if coll["capacity"] is not None:
                cnt = conn.execute(
                    "SELECT COUNT(*) FROM collection_responses WHERE collection_id = ? AND status = 'registered'",
                    (cid,),
                ).fetchone()[0]
                if cnt >= coll["capacity"]:
                    return False, "名额已满，无法报名"
        timestamp = now_str()
        if row is None:
            conn.execute(
                "INSERT INTO collection_responses (collection_id, group_id, member_id, data_json, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'registered', ?, ?)",
                (cid, group_id, member_id, json.dumps(merged, ensure_ascii=False), timestamp, timestamp),
            )
            return True, "报名成功"
        conn.execute(
            "UPDATE collection_responses SET data_json = ?, status = 'registered', updated_at = ?"
            " WHERE id = ?",
            (json.dumps(merged, ensure_ascii=False), timestamp, row[0]),
        )
        return True, "已更新你的报名" if row[1] == "registered" else "报名成功（重新报名）"


def cancel_collection_registration(cid: int, group_id: str, member_id: str) -> tuple[bool, str]:
    """成员取消自己的报名（标记 cancelled，保留记录；不影响其他成员）。"""
    coll = get_collection(cid)
    if coll is None or coll["group_id"] != group_id:
        return False, "征集不存在或不属于本群"
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE collection_responses SET status = 'cancelled', updated_at = ?"
            " WHERE collection_id = ? AND member_id = ? AND status = 'registered'",
            (now_str(), cid, member_id),
        )
        if cur.rowcount == 0:
            return False, "你没有报名过该征集"
        return True, "已取消报名"


def get_collection_registration(cid: int, member_id: str, group_id: str = "") -> dict | None:
    """查询成员在某征集的当前报名（仅 registered）。

    group_id 传入时额外校验征集属于该群（防跨群泄露他人报名）；不匹配返回 None。
    """
    if group_id:
        coll = get_collection(cid)
        if coll is None or coll["group_id"] != group_id:
            return None
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_COLLECTION_RESPONSE_COLS} FROM collection_responses"
            " WHERE collection_id = ? AND member_id = ? AND status = 'registered'",
            (cid, member_id),
        ).fetchone()
    return _row_to_collection_response(row) if row else None


def list_collection_responses(cid: int) -> list[dict]:
    """管理员明细：某征集的全部 registered 报名。"""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_COLLECTION_RESPONSE_COLS} FROM collection_responses"
            " WHERE collection_id = ? AND status = 'registered' ORDER BY id",
            (cid,),
        ).fetchall()
    return [_row_to_collection_response(r) for r in rows]


def summarize_collection(cid: int) -> dict:
    """普通成员汇总：总报名数 / 剩余名额 / 已取消数。不含个人明细。"""
    coll = get_collection(cid)
    if coll is None:
        return {}
    with _connect() as conn:
        reg = conn.execute(
            "SELECT COUNT(*) FROM collection_responses WHERE collection_id = ? AND status = 'registered'",
            (cid,),
        ).fetchone()[0]
        cancelled = conn.execute(
            "SELECT COUNT(*) FROM collection_responses WHERE collection_id = ? AND status = 'cancelled'",
            (cid,),
        ).fetchone()[0]
    return {
        "collection_id": cid,
        "title": coll["title"],
        "purpose": coll["purpose"],
        "status": coll["status"],
        "registered": reg,
        "cancelled": cancelled,
        "capacity": coll["capacity"],
        "remaining": None if coll["capacity"] is None else max(0, coll["capacity"] - reg),
    }


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
    args = [cleaned[c] for c in cols] + [now_str(), cid]
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            f"UPDATE competitions SET {sets}, updated_at = ? WHERE id = ?", args
        )
        if cur.rowcount == 0:
            return False
        if "official_site" in cleaned:
            _sync_competition_source_on(conn, cid, cleaned["official_site"], source_text)
        cur_comp = conn.execute(
            f"SELECT {_COMPETITION_COLS} FROM competitions WHERE id = ?", (cid,)
        ).fetchone()
        _add_version(conn, cid, "amend", _row_to_competition(cur_comp),
                     note or "超管修正", operator)
        conn.execute(
            "DELETE FROM competition_pending_updates WHERE competition_id = ?", (cid,)
        )
        add_audit(operator, "", "amend", "competition", str(cid),
                  "", json.dumps(cleaned, ensure_ascii=False), conn=conn)
        return True


# =====================================================================
# 通用提醒投递（reminder_deliveries）
# =====================================================================

_DELIVERY_COLS = (
    "id, group_id, obj_type, obj_id, remind_type, target_time, attempts,"
    " status, sent_at, last_error, dedup_key, lease_token, lease_until"
)


def _row_to_delivery(r: sqlite3.Row) -> dict:
    return {
        "id": r[0], "group_id": r[1], "obj_type": r[2], "obj_id": r[3],
        "remind_type": r[4], "target_time": r[5], "attempts": r[6],
        "status": r[7], "sent_at": r[8], "last_error": r[9], "dedup_key": r[10],
        "lease_token": r[11], "lease_until": r[12],
    }


def delivery_dedup_key(group_id: str, obj_type: str, obj_id: int,
                       remind_type: str, target_time: str) -> str:
    return f"{group_id}|{obj_type}|{obj_id}|{remind_type}|{_canonical_time(target_time)}"


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


def _migrate_reminder_deliveries(conn: sqlite3.Connection) -> None:
    """补租约字段并合并同对象旧键，保留成功回执与最大尝试次数。"""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(reminder_deliveries)")}
    for column in ("lease_token", "lease_until"):
        if column not in cols:
            conn.execute(f"ALTER TABLE reminder_deliveries ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
    # 成功事实保留原始记录，并为旧时间格式补规范身份；状态投影失败的成功也不能重发。
    for key, token, sent_at in conn.execute(
        "SELECT dedup_key, lease_token, sent_at FROM reminder_success_receipts ORDER BY sent_at"
    ).fetchall():
        canonical = _canonical_delivery_key(key)
        if canonical != key:
            conn.execute(
                "INSERT OR IGNORE INTO reminder_success_receipts (dedup_key, lease_token, sent_at) VALUES (?, ?, ?)",
                (canonical, token, sent_at),
            )
    rows = conn.execute(f"SELECT {_DELIVERY_COLS} FROM reminder_deliveries ORDER BY id").fetchall()
    grouped: dict[tuple, list[dict]] = {}
    for row in rows:
        delivery = _row_to_delivery(row)
        delivery["target_time"] = _canonical_time(delivery["target_time"])
        identity = tuple(delivery[k] for k in ("group_id", "obj_type", "obj_id", "remind_type", "target_time"))
        grouped.setdefault(identity, []).append(delivery)
    rank = {"sent": 6, "sending": 5, "pending": 4, "failed": 3, "expired": 2, "cancelled": 1}
    for identity, deliveries in grouped.items():
        winner = max(deliveries, key=lambda d: (rank.get(d["status"], 0), d["attempts"], -d["id"]))
        for delivery in deliveries:
            if delivery["id"] != winner["id"]:
                conn.execute("DELETE FROM reminder_deliveries WHERE id = ?", (delivery["id"],))
        key = delivery_dedup_key(*identity)
        conn.execute(
            "UPDATE reminder_deliveries SET dedup_key = ?, target_time = ?, attempts = ? WHERE id = ?",
            (key, identity[4], max(d["attempts"] for d in deliveries), winner["id"]),
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_identity ON reminder_deliveries"
        " (group_id, obj_type, obj_id, remind_type, target_time)"
    )
    # 成功事实独立提交；状态投影写入失败时，查询仍显示已发送并保留去重。
    conn.execute("""CREATE VIEW IF NOT EXISTS reminder_delivery_states AS
        SELECT d.id, d.group_id, d.obj_type, d.obj_id, d.remind_type,
               d.target_time, d.attempts,
               CASE WHEN r.dedup_key IS NOT NULL THEN 'sent' ELSE d.status END AS status,
               COALESCE(r.sent_at, d.sent_at) AS sent_at,
               CASE WHEN r.dedup_key IS NOT NULL THEN '' ELSE d.last_error END AS last_error,
               d.dedup_key,
               CASE WHEN r.dedup_key IS NOT NULL THEN '' ELSE d.lease_token END AS lease_token,
               CASE WHEN r.dedup_key IS NOT NULL THEN '' ELSE d.lease_until END AS lease_until
        FROM reminder_deliveries d
        LEFT JOIN reminder_success_receipts r ON r.dedup_key = d.dedup_key
    """)


def _delivery_now(now: datetime | None = None) -> datetime:
    now = now or datetime.now(TZ)
    return now.replace(tzinfo=TZ) if now.tzinfo is None else now.astimezone(TZ)


def _delivery_parse_time(text: str) -> datetime | None:
    if not isinstance(text, str):
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(text.strip(), fmt).replace(tzinfo=TZ)
        except (ValueError, TypeError):
            continue
    return None


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
        if row is None or row[0] != delivery["group_id"] or row[1] != "open" or _canonical_time(row[2]) != delivery["target_time"]:
            return "cancelled"
    elif delivery["obj_type"] == "competition":
        field = {"reg_deadline": "official_reg_deadline", "contest_time": "official_time"}.get(delivery["remind_type"])
        if field is None:
            return "cancelled"
        row = conn.execute(f"SELECT status, {field} FROM competitions WHERE id = ?", (delivery["obj_id"],)).fetchone()
        if row is None or row[0] != "confirmed" or _canonical_time(row[1]) != delivery["target_time"]:
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
    else:
        return "cancelled"
    target = _delivery_parse_time(delivery["target_time"])
    if target is None:
        return "cancelled"
    return "expired" if target <= now else None


def reconcile_deliveries(now: datetime | None = None, *, max_attempts: int = 3,
                         allowed_groups: set[str] | None = None) -> None:
    """终结旧计划，恢复过期租约；活跃发送者保留其回执写入权。"""
    now = _delivery_now(now)
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            f"SELECT {_DELIVERY_COLS} FROM reminder_deliveries"
            " WHERE status IN ('pending', 'sending', 'cancelled')"
            " AND NOT EXISTS (SELECT 1 FROM reminder_success_receipts r"
            " WHERE r.dedup_key = reminder_deliveries.dedup_key)"
        ).fetchall()
        for row in rows:
            delivery = _row_to_delivery(row)
            # 在途发送仍有写回权；对象关闭/改期只能阻止尚未发送的计划。
            if delivery["status"] == "sending":
                until = _delivery_parse_time(delivery["lease_until"])
                if until is not None and until > now and delivery["lease_token"]:
                    continue
            status = _delivery_invalid_status(conn, delivery, now, allowed_groups)
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


def claim_delivery(group_id: str, obj_type: str, obj_id: int, remind_type: str,
                   target_time: str, *, now: datetime | None = None,
                   max_attempts: int = 3, lease_seconds: int = 120,
                   allowed_groups: set[str] | None = None) -> str | None:
    """同一写事务复核对象并领取；计数在领取时递增，进程中断也消耗尝试。"""
    if max_attempts < 1 or lease_seconds < 1:
        raise ValueError("提醒尝试次数和发送租约必须大于零")
    now = _delivery_now(now)
    target_time = _canonical_time(target_time)
    key = delivery_dedup_key(group_id, obj_type, obj_id, remind_type, target_time)
    if allowed_groups is not None and group_id not in allowed_groups:
        return None
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR IGNORE INTO reminder_deliveries (group_id, obj_type, obj_id, remind_type, target_time, dedup_key)"
            " VALUES (?, ?, ?, ?, ?, ?)", (group_id, obj_type, obj_id, remind_type, target_time, key),
        )
        row = conn.execute(f"SELECT {_DELIVERY_COLS} FROM reminder_deliveries WHERE dedup_key = ?", (key,)).fetchone()
        delivery = _row_to_delivery(row)
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
        conn.execute(
            "UPDATE reminder_deliveries SET status = 'sending', attempts = attempts + 1, lease_token = ?, lease_until = ?"
            " WHERE id = ?", (token, until, delivery["id"]),
        )
        return token


def validate_delivery_claim(dedup_key: str, token: str, *, now: datetime | None = None,
                            allowed_groups: set[str] | None = None) -> bool:
    """投递前再检查租约和对象，已关闭/改期的计划不发送。"""
    now = _delivery_now(now)
    with _connect() as conn:
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
    with _connect() as conn:
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
                    now: datetime | None = None, max_attempts: int = 3) -> bool:
    """成功事实先独立提交；状态更新异常不撤销成功，也不会导致租约过期重发。"""
    key = _canonical_delivery_key(dedup_key)
    now_text = _delivery_now(now).strftime("%Y-%m-%d %H:%M:%S")
    if ok and not record_delivery_success(key, token, now=now):
        return False
    try:
        with _connect() as conn:
            cur = conn.execute(
                "UPDATE reminder_deliveries SET status = CASE WHEN ? THEN 'sent' WHEN attempts >= ? THEN 'failed' ELSE 'pending' END,"
                " sent_at = CASE WHEN ? THEN ? ELSE sent_at END, last_error = ?, lease_token = '', lease_until = ''"
                " WHERE dedup_key = ? AND status = 'sending' AND lease_token = ?",
                (ok, max_attempts, ok, now_text, "" if ok else (error or "发送失败"), key, token),
            )
            return ok or cur.rowcount > 0
    except sqlite3.Error:
        if not ok:
            raise
        logger.exception("提醒状态更新失败，已保留成功回执: %s", key)
        return True


def ensure_delivery(group_id: str, obj_type: str, obj_id: int,
                    remind_type: str, target_time: str) -> bool:
    """登记一条待发送提醒（幂等：同去重键只建一次）。返回是否新创建。"""
    target_time = _canonical_time(target_time)
    key = delivery_dedup_key(group_id, obj_type, obj_id, remind_type, target_time)
    with _connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO reminder_deliveries (group_id, obj_type, obj_id,"
            " remind_type, target_time, status, dedup_key)"
            " VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (group_id, obj_type, obj_id, remind_type, target_time, key),
        )
        return cur.rowcount > 0


def get_delivery_by_key(dedup_key: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_DELIVERY_COLS} FROM reminder_delivery_states WHERE dedup_key = ?",
            (_canonical_delivery_key(dedup_key),),
        ).fetchone()
    return _row_to_delivery(row) if row else None


def list_pending_deliveries(limit: int = 200) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_DELIVERY_COLS} FROM reminder_delivery_states"
            " WHERE status = 'pending' ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_delivery(r) for r in rows]


def list_deliveries(group_id: str, *, statuses: tuple[str, ...] | None = None,
                    limit: int = 200) -> list[dict]:
    where = "group_id = ?"
    args: list = [group_id]
    if statuses:
        where += " AND status IN (" + ",".join("?" for _ in statuses) + ")"
        args.extend(statuses)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_DELIVERY_COLS} FROM reminder_delivery_states WHERE {where} ORDER BY id DESC LIMIT ?",
            [*args, limit],
        ).fetchall()
    return [_row_to_delivery(row) for row in rows]


def delivery_status_counts(group_id: str) -> dict[str, int]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM reminder_delivery_states WHERE group_id = ? GROUP BY status", (group_id,)
        ).fetchall()
    return dict(rows)


def record_delivery_attempt(dedup_key: str, ok: bool, error: str = "", *, max_attempts: int = 3) -> None:
    """兼容旧调用：只记录未领取 pending，不能覆盖租约或成功记录。"""
    with _connect() as conn:
        if ok:
            conn.execute(
                "UPDATE reminder_deliveries SET attempts = attempts + 1, status = 'sent',"
                " sent_at = ?, last_error = '' WHERE dedup_key = ? AND status = 'pending'",
                (now_str(), _canonical_delivery_key(dedup_key)),
            )
        else:
            conn.execute(
                "UPDATE reminder_deliveries SET attempts = attempts + 1,"
                " status = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE 'pending' END,"
                " last_error = ? WHERE dedup_key = ? AND status = 'pending'",
                (max_attempts, error or "发送失败", _canonical_delivery_key(dedup_key)),
            )


# =====================================================================
# 审计日志
# =====================================================================

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
              before, after, now_str())
    if conn is not None:
        conn.execute(sql, params)
        return
    with _connect() as conn:
        conn.execute(sql, params)


def list_audit(group_id: str | None = None, limit: int = 50) -> list[dict]:
    with _connect() as conn:
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


# =====================================================================
# 旧库安全迁移
# =====================================================================


def list_unclaimed_contests() -> list[dict]:
    """未归属任何群的旧记录（group_id=''），仅管理员可见并显式迁移。"""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_CONTEST_COLS} FROM contests WHERE group_id = '' ORDER BY id"
        ).fetchall()
    return [_row_to_contest(r) for r in rows]


def claim_contest(contest_id: int, group_id: str) -> bool:
    """将旧记录显式归属到指定群。"""
    group_id = _as_str(group_id)
    with _connect() as conn:
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
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_CONTEST_COLS} FROM contests WHERE id = ?", (contest_id,)
        ).fetchone()
        if row is None:
            return None
        contest = _row_to_contest(row)
        comp_id = _create_competition_on(
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
        add_audit(operator, group_id, "migrate", "contest", str(contest_id),
                  "", f"->competition {comp_id}", conn=conn)
        return comp_id


# =====================================================================
# 公共比赛待审更新（confirmed 比赛的新内容先存待审，审核通过后替换）
# =====================================================================

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
    fields = _clean_competition_patch(fields, ignore_empty=True)
    if not fields:
        raise ValueError("待审更新没有有效字段")
    with _connect() as conn:
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
            (fields_json, source_id, note or "", operator or "", now_str(), row[0]),
        )
        return row[0]
    cur = conn.execute(
        "INSERT INTO competition_pending_updates (competition_id, fields_json,"
        " source_id, change_note, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (cid, fields_json, source_id, note or "", operator or "", now_str(), now_str()),
    )
    return cur.lastrowid


def store_crawled_competition(expected_source: dict, patch: dict, new_source_state: dict) -> dict | None:
    """原子核对来源快照并保存待审/旧草稿及基线；过期结果返回 None，零写入。"""
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not source_snapshot_matches(expected_source, conn=conn):
            return None
        cid = expected_source["competition_id"]
        row = conn.execute(f"SELECT {_COMPETITION_COLS} FROM competitions WHERE id = ?", (cid,)).fetchone()
        if row is None or row[11] not in ("draft", "confirmed"):
            return None
        fields = _clean_competition_patch(patch, ignore_empty=True)
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
                [*fields.values(), now_str(), cid],
            )
            current = conn.execute(f"SELECT {_COMPETITION_COLS} FROM competitions WHERE id = ?", (cid,)).fetchone()
            _add_version(conn, cid, "draft", _row_to_competition(current), "公共来源变化，更新草稿", "crawler:auto")
            item["action"] = "updated"
            status = "draft"
        state = {**new_source_state, "last_status": status, "last_error": ""}
        if not update_source_fetch(expected_source["id"], conn=conn, **state):
            raise ValueError("来源基线保存失败")
        return item


def get_pending_update(competition_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_PENDING_UPDATE_COLS} FROM competition_pending_updates"
            " WHERE competition_id = ?",
            (competition_id,),
        ).fetchone()
    return _row_to_pending_update(row) if row else None


def list_pending_updates() -> list[dict]:
    """列出全部待审更新（join 比赛名，供 /待审列表 使用）。"""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT pu.id, pu.competition_id, pu.fields_json, pu.source_id,"
            " pu.change_note, pu.created_by, pu.created_at, pu.updated_at,"
            " c.name, c.status"
            f" FROM competition_pending_updates pu"
            f" JOIN competitions c ON c.id = pu.competition_id"
            f" ORDER BY pu.id DESC"
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
    with _connect() as conn:
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
            f"SELECT {_COMPETITION_COLS} FROM competitions WHERE id = ?", (comp_id,)
        ).fetchone()
        if comp is None or comp[11] != "confirmed":
            return False
        source_url = _validate_pending_source_on(conn, comp_id, pending["source_id"])
        try:
            fields = json.loads(pending["fields_json"])
        except (ValueError, TypeError):
            raise ValueError("待审内容格式错误") from None
        cleaned = _clean_competition_patch(fields, ignore_empty=True)
        if not cleaned:
            raise ValueError("待审更新没有有效字段")
        if source_url and cleaned.get("official_site", source_url) != source_url:
            raise ValueError("待审官网与抓取来源不一致")
        sets = ", ".join(f"{c} = ?" for c in cleaned)
        args = [cleaned[c] for c in cleaned] + [now_str(), now_str(), comp_id]
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
            f"SELECT {_COMPETITION_COLS} FROM competitions WHERE id = ?", (comp_id,)
        ).fetchone()
        _add_version(conn, comp_id, "confirmed", _row_to_competition(cur_comp),
                     pending["change_note"] or "待审更新通过", operator)
        add_audit(operator, "", "apply_pending_update", "competition",
                  str(comp_id), "", pending["change_note"] or "", conn=conn)
        return True


def reject_pending_update(update_id: int, operator: str = "", note: str = "") -> bool:
    """审核拒绝：删除待审更新，正式内容保持不变。"""
    with _connect() as conn:
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
        add_audit(operator, "", "reject_pending_update", "competition",
                  str(pending["competition_id"]), "",
                  f"拒绝更新：{note or '未说明原因'}", conn=conn)
        return True
