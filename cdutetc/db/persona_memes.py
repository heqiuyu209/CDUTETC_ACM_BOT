"""人设与表情包域数据访问：bot_memory_facts、bot_daily_state、表情包缓存与 dHash 去重。"""

import logging
import sqlite3
from datetime import datetime, timedelta

from cdutetc.db import admin as _admin
from cdutetc.db import connection as _conn

logger = logging.getLogger(__name__)


# ================= 零审核自动收图：群开关 + MD5 去重台账 =================
# 收图默认关闭（默认 0）：不主动开就不碰群友发的图，避免误收隐私内容。
MEME_COLLECT_SETTING = "meme_collect_enabled"


_MEME_COLLECT_ON = "on"


_MEME_COLLECT_OFF = "off"


def set_meme_collect_enabled(group_id: str, enabled: bool, operator: str = "") -> None:
    """按群开关自动收图（默认关闭）。"""
    if type(enabled) is not bool:
        raise ValueError("请明确开启或关闭自动收图")
    _admin.set_group_setting(group_id, MEME_COLLECT_SETTING,
                      _MEME_COLLECT_ON if enabled else _MEME_COLLECT_OFF, operator)


def meme_collect_enabled(group_id: str) -> bool:
    """该群是否开启了自动收图；未设置过、库未初始化都按关闭处理。"""
    try:
        value = _admin.get_group_setting(group_id, MEME_COLLECT_SETTING, _MEME_COLLECT_OFF)
    except sqlite3.OperationalError:
        return False
    return value.strip().lower() == _MEME_COLLECT_ON


_MEME_HASH_COLUMNS = "md5, category, tags, file_path, deleted, created_at, dhash, cleaned"


def _meme_hash_row(row) -> dict | None:
    """把小表行转成字典；列顺序按 _MEME_HASH_COLUMNS，尾部兼容老查询。"""
    if row is None:
        return None
    record = {"md5": row[0], "category": row[1], "tags": row[2], "file_path": row[3],
              "deleted": bool(row[4]), "created_at": row[5]}
    record["dhash"] = (row[6] or "") if len(row) > 6 else ""
    record["cleaned"] = bool(row[7]) if len(row) > 7 else False
    return record


def get_meme_hash(md5: str) -> dict | None:
    """按 MD5 取一条收图台账；不存在返回 None。"""
    value = _conn._as_str(md5)
    if not value:
        return None
    with _conn._connect() as conn:
        row = conn.execute(
            f"SELECT {_MEME_HASH_COLUMNS} FROM meme_hashes WHERE md5 = ?",
            (value,),
        ).fetchone()
    return _meme_hash_row(row)


def meme_hash_exists(md5: str) -> bool:
    """该 MD5 是否已收过（含 deleted=1 的黑名单图，一律视为重复）。

    因分类超限被腾掉（cleaned=1）的记录不算：文件已经删了，同图再出现应当能重新收录。
    """
    record = get_meme_hash(md5)
    if record is None:
        return False
    return not record.get("cleaned", False)


def save_meme_hash(md5: str, category: str, tags: str, file_path: str,
                   dhash: str = "") -> bool:
    """登记一张新收的表情包；MD5 已存在（含黑名单）时不覆盖，返回 False。

    ``dhash`` 是相似图去重用的感知哈希，缺省空串表示「没算出来」，此时只按 MD5 查重。
    唯一例外：该 MD5 之前因分类超限被腾掉（cleaned=1）时复活这条记录，
    因为它的文件早已删除，重新收录并不算重复。
    """
    value = _conn._as_str(md5)
    if not value:
        raise ValueError("收图台账需要 MD5")
    with _conn._connect() as conn:
        params = (value, _conn._as_str(category), _conn._as_str(tags), _conn._as_str(file_path),
                  _conn.now_str(), _conn._as_str(dhash))
        cur = conn.execute(
            "INSERT INTO meme_hashes"
            " (md5, category, tags, file_path, deleted, created_at, dhash)"
            " VALUES (?, ?, ?, ?, 0, ?, ?) ON CONFLICT(md5) DO NOTHING",
            params,
        )
        if cur.rowcount > 0:
            return True
        row = conn.execute("SELECT cleaned FROM meme_hashes WHERE md5 = ?",
                           (value,)).fetchone()
        if not row or not row[0]:
            return False  # 真的重复（或已进黑名单）：保持原记录不动
        # 之前被分类上限腾掉的记录：整行重建而不是就地更新，让 created_at/rowid
        # 一起排到队尾，否则复活的老记录会立刻又被当成「最旧」淘汰掉。
        conn.execute("DELETE FROM meme_hashes WHERE md5 = ?", (value,))
        conn.execute(
            "INSERT INTO meme_hashes"
            " (md5, category, tags, file_path, deleted, created_at, dhash)"
            " VALUES (?, ?, ?, ?, 0, ?, ?)",
            params,
        )
        return True


def mark_meme_hash_deleted(md5: str, *, file_path: str = "") -> bool:
    """把一张已收的图拉黑（deleted=1），此后同 MD5 不再收录。"""
    value = _conn._as_str(md5)
    if not value:
        return False
    with _conn._connect() as conn:
        cur = conn.execute(
            "UPDATE meme_hashes SET deleted = 1, file_path = COALESCE(NULLIF(?, ''), file_path)"
            " WHERE md5 = ?",
            (_conn._as_str(file_path), value),
        )
        return cur.rowcount > 0


def mark_meme_hash_cleaned(md5: str) -> bool:
    """标记一条记录「因分类超限已腾出容量」。

    与 :func:`mark_meme_hash_deleted` 的区别：不进黑名单，同 MD5/相似图再次出现仍可收录；
    仅让 count_meme_hashes 与相似图去重不再把它算作现存素材。
    """
    value = _conn._as_str(md5)
    if not value:
        return False
    with _conn._connect() as conn:
        cur = conn.execute(
            "UPDATE meme_hashes SET cleaned = 1 WHERE md5 = ?",
            (value,),
        )
        return cur.rowcount > 0


def find_meme_hash_by_path(file_path: str) -> dict | None:
    """按本地相对路径反查台账（/删图 用路径找到 MD5 再拉黑）。"""
    relative = _conn._as_str(file_path)
    if not relative:
        return None
    with _conn._connect() as conn:
        row = conn.execute(
            f"SELECT {_MEME_HASH_COLUMNS} FROM meme_hashes"
            " WHERE file_path = ? ORDER BY created_at DESC LIMIT 1",
            (relative,),
        ).fetchone()
    return _meme_hash_row(row)


def list_meme_dhashes(*, include_deleted: bool = True) -> list[dict]:
    """列出可用于相似图去重的感知哈希（``[{"md5", "dhash"}]``）。

    默认包含 deleted=1 的黑名单图——删过的图换个压缩再发一次也不该被回收；
    但已因分类超限腾掉（cleaned=1）的记录不参与，否则同一张图再也收不回来。
    空 dhash（老库/算不出）不返回，避免误判。
    """
    sql = ("SELECT md5, dhash FROM meme_hashes"
           " WHERE dhash != '' AND cleaned = 0")
    if not include_deleted:
        sql += " AND deleted = 0"
    with _conn._connect() as conn:
        rows = conn.execute(sql).fetchall()
    return [{"md5": row[0], "dhash": row[1]} for row in rows]


def oldest_meme_hashes(category: str, *, limit: int = 1) -> list[dict]:
    """取某分类最旧的若干条现存记录（FIFO 淘汰用），按 created_at 正序。"""
    clean = _conn._as_str(category)
    if not clean:
        return []
    count = max(1, int(limit or 1))
    with _conn._connect() as conn:
        rows = conn.execute(
            "SELECT md5, file_path, created_at FROM meme_hashes"
            " WHERE category = ? AND deleted = 0 AND cleaned = 0"
            " ORDER BY created_at ASC, rowid ASC LIMIT ?",
            (clean, count),
        ).fetchall()
    return [{"md5": row[0], "file_path": row[1], "created_at": row[2]} for row in rows]


def count_meme_hashes(*, category: str = "", include_deleted: bool = False) -> int:
    """统计现有素材数量（可按分类过滤），供 /管理、/偷图 回执与 FIFO 淘汰判定。"""
    sql = "SELECT COUNT(*) FROM meme_hashes WHERE 1 = 1"
    params: list = []
    if not include_deleted:
        sql += " AND deleted = 0 AND cleaned = 0"
    clean = _conn._as_str(category)
    if clean:
        sql += " AND category = ?"
        params.append(clean)
    with _conn._connect() as conn:
        row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row else 0


# ================= 官方富媒体表情包 file_info 缓存 =================
# 平台上传接口会返回 file_info 的 ttl（秒）；这里记录绝对过期时刻，
# 过期即视为未命中，由调用方重新上传，绝不复用可能已失效的 file_info。
# QQ 官方给的 file_info 有效期是 3 天，取 24 小时（86400）留足余量：
# 既躲开「拿失效 file_info 发图」的坑，又把一天内重复发同一张图的再次上传全省掉。
MEME_FILE_CACHE_TTL = 86400


def get_meme_file_cache(group_openid: str, relative_path: str, *,
                        now: datetime | None = None) -> str:
    """取有效期内的 file_info；不存在或已过期返回空串，由调用方重新上传。"""
    group = _conn._as_str(group_openid)
    relative = _conn._as_str(relative_path)
    if not group or not relative:
        return ""
    moment = now or datetime.now(_conn.TZ)
    with _conn._connect() as conn:
        row = conn.execute(
            "SELECT file_info, expires_at FROM meme_file_cache"
            " WHERE group_openid = ? AND relative_path = ?",
            (group, relative),
        ).fetchone()
    if not row:
        return ""
    file_info, expires_at = str(row[0] or ""), str(row[1] or "")
    if not file_info or not expires_at:
        return ""
    try:
        deadline = datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_conn.TZ)
    except ValueError:
        return ""
    return file_info if deadline > moment else ""


def save_meme_file_cache(group_openid: str, relative_path: str, file_info: str, *,
                         ttl_seconds: int = MEME_FILE_CACHE_TTL,
                         now: datetime | None = None) -> bool:
    """写入/刷新 file_info 缓存；ttl 非正数时用默认有效期兜底。"""
    group = _conn._as_str(group_openid)
    relative = _conn._as_str(relative_path)
    info = _conn._as_str(file_info)
    if not group or not relative or not info:
        return False
    ttl = _conn._as_int(ttl_seconds)
    if not ttl or ttl <= 0:
        ttl = MEME_FILE_CACHE_TTL
    moment = now or datetime.now(_conn.TZ)
    expires_at = (moment + timedelta(seconds=ttl)).strftime("%Y-%m-%d %H:%M:%S")
    with _conn._connect() as conn:
        conn.execute(
            "INSERT INTO meme_file_cache"
            " (group_openid, relative_path, file_info, expires_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(group_openid, relative_path) DO UPDATE SET"
            " file_info = excluded.file_info, expires_at = excluded.expires_at,"
            " updated_at = excluded.updated_at",
            (group, relative, info, expires_at,
             moment.strftime("%Y-%m-%d %H:%M:%S")),
        )
    return True


def purge_meme_file_cache(*, group_openid: str = "", now: datetime | None = None) -> int:
    """清理过期（或指定群的）file_info 缓存，返回删除条数。"""
    moment = (now or datetime.now(_conn.TZ)).strftime("%Y-%m-%d %H:%M:%S")
    group = _conn._as_str(group_openid)
    sql = "DELETE FROM meme_file_cache WHERE expires_at <= ?"
    args: list = [moment]
    if group:
        sql += " AND group_openid = ?"
        args.append(group)
    with _conn._connect() as conn:
        return conn.execute(sql, args).rowcount


# ================= 拟人化聊天的三层持久化记忆 =================
# 单个成员在单个群最多保留的记忆标签条数：超限自动淘汰最旧，防止越记越长。
MEMORY_FACTS_KEEP = 5


MEMORY_FACT_MAX_CHARS = 15


DEFAULT_DAILY_STATUS = "在机房吹空调摸鱼中"


def add_memory_fact(group_openid: str, user_openid: str, fact: str, *,
                    keep: int = MEMORY_FACTS_KEEP,
                    now: datetime | None = None) -> bool:
    """写一条个人特征标签；同一条标签只留最新时刻，超量淘汰最旧。

    标签由 0 Token 规则从群聊原话里截取，因此这里只接受短文本：
    空标签、超长标签一律拒收（返回 False），不做静默截断。
    """
    group = _conn._as_str(group_openid)
    user = _conn._as_str(user_openid)
    text = _conn._as_str(fact)
    if not group or not user or not text:
        return False
    if len(text) > MEMORY_FACT_MAX_CHARS:
        return False
    limit = _conn._as_int(keep)
    if not limit or limit <= 0:
        limit = MEMORY_FACTS_KEEP
    moment = now or datetime.now(_conn.TZ)
    stamp = moment.strftime("%Y-%m-%d %H:%M:%S")
    with _conn._connect() as conn:
        # 同一条标签重复出现只刷新时间，避免「我叫小明」刷十次占满 5 个槽位。
        existing = conn.execute(
            "SELECT id FROM bot_memory_facts"
            " WHERE group_openid = ? AND user_openid = ? AND fact = ?",
            (group, user, text),
        ).fetchone()
        if existing:
            conn.execute("UPDATE bot_memory_facts SET updated_at = ? WHERE id = ?",
                         (stamp, existing[0]))
        else:
            conn.execute(
                "INSERT INTO bot_memory_facts (group_openid, user_openid, fact, updated_at)"
                " VALUES (?, ?, ?, ?)",
                (group, user, text, stamp),
            )
        # 淘汰超出保留条数的最旧记录（多余的历史标签没有价值，只占 Prompt 预算）。
        conn.execute(
            "DELETE FROM bot_memory_facts WHERE id IN ("
            " SELECT id FROM bot_memory_facts WHERE group_openid = ? AND user_openid = ?"
            " ORDER BY updated_at DESC, id DESC LIMIT -1 OFFSET ?)",
            (group, user, limit),
        )
    return True


def list_memory_facts(group_openid: str, user_openid: str, *,
                      limit: int = 3) -> list[str]:
    """按最近更新倒序取个人标签；limit 非正数时返回空列表（调用方明确不要记忆）。"""
    group = _conn._as_str(group_openid)
    user = _conn._as_str(user_openid)
    count = _conn._as_int(limit)
    if not group or not user or not count or count <= 0:
        return []
    with _conn._connect() as conn:
        # 取出该用户的全部标签（每人最多 MEMORY_FACTS_KEEP 条，代价可忽略）：
        # 先 LIMIT 再去重会让重复脏行占掉名额，把真正有效的旧标签挤出去。
        rows = conn.execute(
            "SELECT fact FROM bot_memory_facts"
            " WHERE group_openid = ? AND user_openid = ?"
            " ORDER BY updated_at DESC, id DESC",
            (group, user),
        ).fetchall()
    facts: list[str] = []
    for row in rows:
        value = str(row[0]) if row and row[0] else ""
        # 写入侧已保证同标签只留一条，这里再兜一层：历史脏数据不至于挤占 Prompt 预算。
        if value and value not in facts:
            facts.append(value)
    return facts[:count]


def clear_memory_facts(*, group_openid: str = "", user_openid: str = "") -> int:
    """清空记忆标签（运维/测试用）；不传条件即清空全表，返回删除条数。"""
    sql = "DELETE FROM bot_memory_facts WHERE 1 = 1"
    args: list = []
    group = _conn._as_str(group_openid)
    user = _conn._as_str(user_openid)
    if group:
        sql += " AND group_openid = ?"
        args.append(group)
    if user:
        sql += " AND user_openid = ?"
        args.append(user)
    with _conn._connect() as conn:
        return conn.execute(sql, args).rowcount


def get_daily_state(*, default_text: str = DEFAULT_DAILY_STATUS,
                    now: datetime | None = None) -> dict:
    """读机器人当前生活状态；库里还没有记录时就地初始化一条默认状态。

    Returns:
        {"status_text": str, "updated_at": str}，永不为 None。
    """
    fallback = _conn._as_str(default_text) or DEFAULT_DAILY_STATUS
    with _conn._connect() as conn:
        row = conn.execute(
            "SELECT status_text, updated_at FROM bot_daily_state WHERE id = 1"
        ).fetchone()
        if row:
            return {"status_text": str(row[0] or "") or fallback,
                    "updated_at": str(row[1] or "")}
        stamp = (now or datetime.now(_conn.TZ)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO bot_daily_state (id, status_text, updated_at) VALUES (1, ?, ?)"
            " ON CONFLICT(id) DO NOTHING",
            (fallback, stamp),
        )
        return {"status_text": fallback, "updated_at": stamp}


def set_daily_state(status_text: str, *, now: datetime | None = None) -> bool:
    """覆盖机器人当前生活状态（单行 upsert）；空文案视为无效写入。"""
    text = _conn._as_str(status_text)
    if not text:
        return False
    stamp = (now or datetime.now(_conn.TZ)).strftime("%Y-%m-%d %H:%M:%S")
    with _conn._connect() as conn:
        conn.execute(
            "INSERT INTO bot_daily_state (id, status_text, updated_at) VALUES (1, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET status_text = excluded.status_text,"
            " updated_at = excluded.updated_at",
            (text, stamp),
        )
    return True
