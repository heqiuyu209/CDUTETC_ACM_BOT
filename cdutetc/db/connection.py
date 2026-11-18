"""数据库连接层：连接管理、事务封装（with _connect()）、WAL、建表与迁移 schema。"""

import logging
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from cdutetc.config import DB_PATH

from cdutetc.db import sources as _sources
from cdutetc.db import training as _training

logger = logging.getLogger(__name__)


TZ = ZoneInfo("Asia/Shanghai")


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
_PENDING_PUBLICATIONS_DDL = """CREATE TABLE IF NOT EXISTS pending_publications (
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


# 运行时白名单：超管用 /加群 /加老师 动态扩充，与 .env 基线取并集。
# 超管白名单不在此表，只能通过配置文件指定，避免运行时提权失控。
_RUNTIME_WHITELIST_DDL = """
CREATE TABLE IF NOT EXISTS runtime_allowed_groups (
    group_id    TEXT PRIMARY KEY,
    note        TEXT NOT NULL DEFAULT '',
    added_by    TEXT NOT NULL DEFAULT '',
    added_at    TEXT NOT NULL DEFAULT ''
)
"""


_RUNTIME_TEACHER_DDL = """
CREATE TABLE IF NOT EXISTS runtime_teachers (
    user_id     TEXT PRIMARY KEY,
    note        TEXT NOT NULL DEFAULT '',
    added_by    TEXT NOT NULL DEFAULT '',
    added_at    TEXT NOT NULL DEFAULT ''
)
"""


# 比赛的通知群范围：有记录 = 只通知这些群；无记录 = 通知全部允许群（默认）。
# 用于把「周赛」限制在集训队群，避免发到新生群。
_COMPETITION_NOTIFY_GROUPS_DDL = """
CREATE TABLE IF NOT EXISTS competition_notify_groups (
    competition_id  INTEGER NOT NULL,
    group_id        TEXT NOT NULL,
    PRIMARY KEY (competition_id, group_id)
)
"""


# 标识别名：官方机器人只给 openid（一串哈希），管理员可给它起个名字，
# 之后所有展示（待确认发布、群列表等）都用名字，避免看不懂。
_ACCOUNT_LABELS_DDL = """
CREATE TABLE IF NOT EXISTS account_labels (
    kind        TEXT NOT NULL,
    account_id  TEXT NOT NULL,
    label       TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (kind, account_id)
)
"""


# 实体别名：官方协议只给哈希 openid，群号/QQ号拿不回来，只能由管理员认领后落到这里。
# 与 account_labels 的区别：这里额外记真实群号/QQ号（numeric_id），
# account_labels 只记名字，供历史命令与既有渲染继续使用。
_ENTITY_ALIASES_DDL = """
CREATE TABLE IF NOT EXISTS entity_aliases (
    entity_type   TEXT NOT NULL,
    openid        TEXT NOT NULL,
    numeric_id    TEXT NOT NULL DEFAULT '',
    display_name  TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (entity_type, openid)
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
    requirement      TEXT NOT NULL DEFAULT '',
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
    scheduled_at TEXT NOT NULL DEFAULT '',
    expires_at  TEXT NOT NULL DEFAULT '',
    error_code  TEXT NOT NULL DEFAULT '',
    object_snapshot TEXT NOT NULL DEFAULT '',
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


_GROUP_NOTIFICATION_BLOCKS_DDL = """
CREATE TABLE IF NOT EXISTS group_notification_blocks (
    protocol      TEXT NOT NULL,
    group_id      TEXT NOT NULL,
    error_code    TEXT NOT NULL,
    platform_code INTEGER,
    blocked_at    TEXT NOT NULL,
    retry_after   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (protocol, group_id)
)
"""


_GROUP_NOTIFICATION_SETTINGS_DDL = """
CREATE TABLE IF NOT EXISTS group_notification_settings (
    protocol       TEXT NOT NULL,
    group_id       TEXT NOT NULL,
    message_format TEXT NOT NULL CHECK(message_format IN ('text', 'markdown')),
    tested_at      TEXT NOT NULL,
    PRIMARY KEY (protocol, group_id)
)
"""


_GROUP_SETTINGS_DDL = """
CREATE TABLE IF NOT EXISTS group_settings (
    group_id    TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT DEFAULT '',
    updated_at  TEXT DEFAULT '',
    PRIMARY KEY (group_id, key)
)
"""


_CUSTOM_REMINDERS_DDL = """
CREATE TABLE IF NOT EXISTS custom_reminders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id     TEXT NOT NULL,
    content      TEXT NOT NULL DEFAULT '',
    target_time  TEXT NOT NULL DEFAULT '',
    repeat_rule  TEXT NOT NULL DEFAULT 'none',
    weekday      TEXT NOT NULL DEFAULT '',
    time_of_day  TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'pending',
    created_by   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL DEFAULT ''
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


_ADMIN_WORKSPACES_DDL = """
CREATE TABLE IF NOT EXISTS admin_workspaces (
    user_openid   TEXT PRIMARY KEY,
    group_openid  TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL DEFAULT ''
)
"""


# 老师自然语言通知中「时间模糊」的那部分：先落库成待补全条目，
# 私聊提醒老师/超管补时间，绝不拿猜测的时间去排提醒。
_TEACHER_INTENT_PENDING_DDL = """
CREATE TABLE IF NOT EXISTS teacher_intent_pending (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    TEXT NOT NULL,
    sender_id   TEXT NOT NULL DEFAULT '',
    msg_id      TEXT NOT NULL DEFAULT '',
    raw_text    TEXT NOT NULL DEFAULT '',
    intent      TEXT NOT NULL DEFAULT '',
    slots_json  TEXT NOT NULL DEFAULT '{}',
    missing     TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending',
    resolved_by TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL DEFAULT '',
    UNIQUE (group_id, msg_id)
)
"""


# 官方富媒体表情包的 file_info 缓存：上传接口按群限流，同一张图在同一群
# 有效期内直接复用 file_info，避免每次发图都重新走一遍上传。
_MEME_FILE_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS meme_file_cache (
    group_openid  TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    file_info     TEXT NOT NULL DEFAULT '',
    expires_at    TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (group_openid, relative_path)
)
"""


# 自动收图去重表：MD5 主键天然唯一，删过的图保留 deleted=1 当黑名单，
# 防止「删了又被别人重发一遍收回来」。tags 存命中的关键词（文件名前缀）。
_MEME_HASHES_DDL = """
CREATE TABLE IF NOT EXISTS meme_hashes (
    md5        TEXT PRIMARY KEY,
    category   TEXT NOT NULL DEFAULT '',
    tags       TEXT NOT NULL DEFAULT '',
    file_path  TEXT NOT NULL DEFAULT '',
    deleted    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT '',
    dhash      TEXT NOT NULL DEFAULT '',
    cleaned    INTEGER NOT NULL DEFAULT 0
)
"""


# 三层持久化记忆之一：个人特征标签。每 (群, 成员) 只保留最近若干条短标签，
# 由 0 Token 规则从群聊原话里截取，绝不调模型；超限由写入方自动淘汰最旧。
_BOT_MEMORY_FACTS_DDL = """
CREATE TABLE IF NOT EXISTS bot_memory_facts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    group_openid TEXT NOT NULL,
    user_openid  TEXT NOT NULL,
    fact         TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL DEFAULT ''
)
"""


# 三层持久化记忆之二：机器人当前生活状态（单行）。仅一行，主键固定为 1，
# 由每日三次定时状态机换文案，供拟人聊天 Prompt 注入一句话当前状态。
_BOT_DAILY_STATE_DDL = """
CREATE TABLE IF NOT EXISTS bot_daily_state (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    status_text TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL DEFAULT ''
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
    """CREATE TABLE IF NOT EXISTS group_competition_archives (
        group_id TEXT NOT NULL, competition_id INTEGER NOT NULL,
        reason TEXT NOT NULL DEFAULT '', archived_at TEXT NOT NULL DEFAULT '',
        archived_by TEXT NOT NULL DEFAULT '', PRIMARY KEY(group_id,competition_id))""",
    """CREATE TABLE IF NOT EXISTS group_test_records (
        group_id TEXT NOT NULL, object_type TEXT NOT NULL, object_id INTEGER NOT NULL,
        marked_at TEXT NOT NULL DEFAULT '', marked_by TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(group_id,object_type,object_id))""",
    """CREATE TABLE IF NOT EXISTS group_weekly_settings (
        group_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
        updated_by TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS group_teacher_assignments (
        group_id TEXT NOT NULL, user_id TEXT NOT NULL,
        updated_by TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(group_id,user_id))""",
    _MAIN_DDL, _REMIND_DDL,
    _COMPETITIONS_DDL, _WEEKLY_CONTEST_RUNS_DDL, _PUBLICATION_CLAIMS_DDL,
    _PENDING_PUBLICATIONS_DDL,
    _RUNTIME_WHITELIST_DDL,
    _RUNTIME_TEACHER_DDL,
    _COMPETITION_NOTIFY_GROUPS_DDL,
    _ACCOUNT_LABELS_DDL, _ENTITY_ALIASES_DDL,
    _COMPETITION_SOURCES_DDL, _COMPETITION_VERSIONS_DDL,
    _PENDING_UPDATES_DDL,
    _ORG_EVENTS_DDL,
    _TRAINING_SESSIONS_DDL, _GROUP_FAQ_DDL, _UNANSWERED_DDL,
    _COLLECTIONS_DDL, _COLLECTION_RESPONSES_DDL,
    _REMINDER_DELIVERIES_DDL, _REMINDER_SUCCESS_RECEIPTS_DDL,
    _GROUP_NOTIFICATION_BLOCKS_DDL, _GROUP_NOTIFICATION_SETTINGS_DDL,
    _GROUP_SETTINGS_DDL, _CUSTOM_REMINDERS_DDL, _AUDIT_LOG_DDL,
    _ADMIN_WORKSPACES_DDL, _TEACHER_INTENT_PENDING_DDL,
    _MEME_FILE_CACHE_DDL,
    _MEME_HASHES_DDL,
    # 机器人记忆两张表：属长期记忆，重置业务数据时保留（见 reset_business_data）。
    _BOT_MEMORY_FACTS_DDL, _BOT_DAILY_STATE_DDL,
)


def _ensure_wal(conn: sqlite3.Connection) -> None:
    """把库切到 WAL。切模式要拿独占锁，另一个进程正在初始化时 SQLite 会直接返回
    ``database is locked``（这条 PRAGMA 不走 busy_timeout），所以这里自己做有限重试。"""
    deadline = time.monotonic() + 10.0
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if ("locked" not in message and "busy" not in message) or time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def _connect() -> sqlite3.Connection:
    """建立连接：显式 10s 忙等 + WAL，避免并发写/长事务直接抛 database is locked。

    - ``timeout=10.0``：连接级忙等阈值（等价于 sqlite3_busy_timeout）；
    - ``journal_mode=WAL``：读写并发，写事务不再阻塞读者；
    - ``synchronous=NORMAL``：WAL 下的推荐档位，断电仅可能丢最后一个事务；
    - ``busy_timeout=10000``：显式声明，防止外部用 PRAGMA 覆盖后回退到默认值。

    三条 PRAGMA 都在事务开启前执行且幂等，已处于 WAL 的库只回读不重写。
    """
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    _ensure_wal(conn)
    return conn


# 供 init_db 判断 _connect 是否被替换（测试里常注入同一条共享内存连接）。
_CONNECT_IMPL = _connect


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
    parsed = _training._delivery_parse_time(text)
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
                    _sources._sync_competition_source_on(conn, cid, url.strip())
    except Exception:
        conn.execute("ROLLBACK TO source_activity_migration")
        conn.execute("RELEASE source_activity_migration")
        raise
    conn.execute("RELEASE source_activity_migration")


def _migrate_training_requirement(conn: sqlite3.Connection) -> None:
    """为旧库的 training_sessions 补 requirement 列（幂等）。

    SQLite 不支持 ADD COLUMN IF NOT EXISTS，因此先用 PRAGMA 查列；
    查询与 ALTER 处在同一写事务内，避免两个进程同时启动时重复补列报错。
    """
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(training_sessions)")}
    if "requirement" not in columns:
        conn.execute("ALTER TABLE training_sessions ADD COLUMN requirement TEXT NOT NULL DEFAULT ''")


def _migrate_group_switches(conn: sqlite3.Connection) -> None:
    """分群细粒度开关的幂等保障。

    group_settings 是键值表，四个开关以行形式存储，缺行即按默认开启处理，
    因此这里只校验表结构可用（PRAGMA 检查），不做列变更，重复执行无副作用。
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(group_settings)")}
    if not {"group_id", "key", "value"} <= columns:
        logger.warning("group_settings 表结构不完整，分群开关使用默认值")


def _migrate_meme_hash_columns(conn: sqlite3.Connection) -> None:
    """为旧库的 meme_hashes 补 dhash / cleaned 列（幂等）。

    dhash 支撑「相似图去重」（汉明距离），cleaned 标记「因分类超限被腾容量的图」：
    这类记录只表示文件已删，不算黑名单，同图再次出现仍可收录。
    """
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(meme_hashes)")}
    if "dhash" not in columns:
        conn.execute("ALTER TABLE meme_hashes ADD COLUMN dhash TEXT NOT NULL DEFAULT ''")
    if "cleaned" not in columns:
        conn.execute("ALTER TABLE meme_hashes ADD COLUMN cleaned INTEGER NOT NULL DEFAULT 0")


def init_db() -> None:
    """初始化数据库：建表 + 旧库迁移 + 索引。程序启动时调用一次。"""
    conn = _connect()
    # 真实 _connect() 每次新建独占连接，初始化后必须关闭：
    # 残留连接会一直持有 -shm 句柄，导致后续切换 journal_mode 或清理时报
    # database is locked。测试注入的共享连接由测试自己管理，这里不代关。
    owns_conn = _connect is _CONNECT_IMPL
    try:
        with conn:
            # WAL 是写进库头的持久属性，必须在事务外执行（事务内会被静默拒绝）。
            # 新库首次初始化、旧库沿用既有模式两种情况都重跑一次以保证落盘生效。
            _ensure_wal(conn)
            # 查列与 ALTER 必须位于同一写事务，避免两个启动进程同时补列。
            # 调用方已有事务时保留它；普通新连接先取得数据库写锁再读 schema。
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            for ddl in _ALL_DDL:
                conn.execute(ddl)
            _migrate_v1(conn)
            _migrate_business_times(conn)
            _migrate_reminder_deliveries(conn)
            _migrate_training_requirement(conn)
            _migrate_group_switches(conn)
            _migrate_meme_hash_columns(conn)
            for ddl in _INDEX_DDL:
                conn.execute(ddl)
            _migrate_source_activity(conn)
            block_columns = {r[1] for r in conn.execute("PRAGMA table_info(group_notification_blocks)")}
            if "retry_after" not in block_columns:
                conn.execute("ALTER TABLE group_notification_blocks ADD COLUMN retry_after TEXT NOT NULL DEFAULT ''")
    finally:
        if owns_conn:
            conn.close()


def _migrate_reminder_deliveries(conn: sqlite3.Connection) -> None:
    """补租约字段并合并同对象旧键，保留成功回执与最大尝试次数。"""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(reminder_deliveries)")}
    for column in ("lease_token", "lease_until", "scheduled_at", "expires_at", "error_code", "object_snapshot"):
        if column not in cols:
            conn.execute(f"ALTER TABLE reminder_deliveries ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
    # 成功事实保留原始记录，并为旧时间格式补规范身份；状态投影失败的成功也不能重发。
    for key, token, sent_at in conn.execute(
        "SELECT dedup_key, lease_token, sent_at FROM reminder_success_receipts ORDER BY sent_at"
    ).fetchall():
        canonical = _training._canonical_delivery_key(key)
        if canonical != key:
            conn.execute(
                "INSERT OR IGNORE INTO reminder_success_receipts (dedup_key, lease_token, sent_at) VALUES (?, ?, ?)",
                (canonical, token, sent_at),
            )
    conn.execute("DROP INDEX IF EXISTS idx_delivery_identity")
    rows = conn.execute(f"SELECT {_training._DELIVERY_COLS} FROM reminder_deliveries ORDER BY id").fetchall()
    grouped: dict[tuple, list[dict]] = {}
    for row in rows:
        delivery = _training._row_to_delivery(row)
        delivery["target_time"] = _canonical_time(delivery["target_time"])
        key = _training._canonical_delivery_key(delivery["dedup_key"])
        parts = key.split("|", 5)
        offset_suffix = parts[5] if len(parts) == 6 else ""
        identity = tuple(delivery[k] for k in ("group_id", "obj_type", "obj_id", "remind_type", "target_time"))
        grouped.setdefault((*identity, offset_suffix), []).append(delivery)
    rank = {"sent": 6, "sending": 5, "pending": 4, "failed": 3, "expired": 2, "cancelled": 1}
    for identity_with_offset, deliveries in grouped.items():
        identity, offset_suffix = identity_with_offset[:5], identity_with_offset[5]
        winner = max(deliveries, key=lambda d: (rank.get(d["status"], 0), d["attempts"], -d["id"]))
        for delivery in deliveries:
            if delivery["id"] != winner["id"]:
                conn.execute("DELETE FROM reminder_deliveries WHERE id = ?", (delivery["id"],))
        key = _training.delivery_dedup_key(*identity)
        if offset_suffix:
            key = f"{key}|{offset_suffix}"
        conn.execute(
            "UPDATE reminder_deliveries SET dedup_key = ?, target_time = ?, attempts = ? WHERE id = ?",
            (key, identity[4], max(d["attempts"] for d in deliveries), winner["id"]),
        )
    identity_key = "group_id || '|' || obj_type || '|' || obj_id || '|' || remind_type || '|' || target_time"
    offset_expr = f"substr(dedup_key, length({identity_key}) + 2)"
    offset_prefix = f"substr(dedup_key, 1, length({identity_key}) + 1) = {identity_key} || '|'"
    conn.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_identity ON reminder_deliveries"
        f" (group_id, obj_type, obj_id, remind_type, target_time,"
        f" CASE WHEN {offset_prefix} AND {offset_expr} <> '' AND {offset_expr} NOT GLOB '*[^0-9.]*'"
        f" THEN CAST({offset_expr} AS REAL) ELSE '' END)"
    )
    # 成功事实独立提交；状态投影写入失败时，查询仍显示已发送并保留去重。
    # Recreate the projection view so existing installations expose migrated columns.
    conn.execute("DROP VIEW IF EXISTS reminder_delivery_states")
    conn.execute("""CREATE VIEW IF NOT EXISTS reminder_delivery_states AS
        SELECT d.id, d.group_id, d.obj_type, d.obj_id, d.remind_type,
               d.target_time, d.attempts,
               CASE WHEN r.dedup_key IS NOT NULL THEN 'sent' ELSE d.status END AS status,
               COALESCE(r.sent_at, d.sent_at) AS sent_at,
               CASE WHEN r.dedup_key IS NOT NULL THEN '' ELSE d.last_error END AS last_error,
               d.dedup_key,
               CASE WHEN r.dedup_key IS NOT NULL THEN '' ELSE d.lease_token END AS lease_token,
               CASE WHEN r.dedup_key IS NOT NULL THEN '' ELSE d.lease_until END AS lease_until,
                d.scheduled_at, d.expires_at,
                CASE WHEN r.dedup_key IS NOT NULL THEN '' ELSE d.error_code END AS error_code,
                d.object_snapshot
        FROM reminder_deliveries d
        LEFT JOIN reminder_success_receipts r ON r.dedup_key = d.dedup_key
    """)
