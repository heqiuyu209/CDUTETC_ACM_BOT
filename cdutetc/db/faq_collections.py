"""问答与征集域数据访问：group_faq、unanswered_questions、collections 与报名登记。"""

import logging
import json
import sqlite3
from cdutetc import config, content_filter

from cdutetc.db import admin as _admin
from cdutetc.db import connection as _conn
from cdutetc.db import contests as _contests
from cdutetc.db import training as _training

logger = logging.getLogger(__name__)


_FAQ_COLS = "id, group_id, question, answer, source, status, updated_at"

# group_faq.status 生命周期：
#   pending   老师群内规则通知自动提炼出来的草稿，等本人私聊确认；
#   confirmed 已生效，问答检索/上下文注入只认这一种；
#   rejected  老师驳回的草稿，保留审计但不参与任何召回；
#   deleted   管理端删除（软删），同样不参与召回。
FAQ_PENDING = "pending"
FAQ_CONFIRMED = "confirmed"
FAQ_REJECTED = "rejected"
FAQ_DELETED = "deleted"

# 老师规则通知提炼出的草稿，source 记「teacher_intent:<提交人>」。
# 这样私聊确认时能按提交人筛出自己的草稿，不必为了归属人多加一列。
FAQ_SOURCE_TEACHER = "teacher_intent"


def teacher_source(operator: str) -> str:
    """规则草稿的 source 标记：带提交人，供私聊确认时按人过滤。"""
    return f"{FAQ_SOURCE_TEACHER}:{_conn._as_str(operator)}"


def _row_to_faq(r: sqlite3.Row) -> dict:
    return {
        "id": r[0], "group_id": r[1], "question": r[2], "answer": r[3],
        "source": r[4], "status": r[5], "updated_at": r[6],
    }


def _close_same_question(conn, group_id: str, question: str, faq_id: int,
                         *, skip_qid: int | None = None) -> None:
    """FAQ 生效时顺手关掉本群其它同题未答问题（调用方保证已在事务内）。"""
    target = _contests._normalize_question(question)
    rows = conn.execute(
        "SELECT id, question FROM unanswered_questions WHERE group_id = ? AND status = 'open'",
        (group_id,),
    ).fetchall()
    for qid, stored_question in rows:
        if qid == skip_qid:
            continue
        if _contests._normalize_question(stored_question) == target:
            conn.execute(
                "UPDATE unanswered_questions SET status = 'answered', faq_id = ?"
                " WHERE id = ? AND group_id = ? AND status = 'open'",
                (faq_id, qid, group_id),
            )


def add_faq(group_id: str, question: str, answer: str, source: str = "",
            operator: str = "") -> int:
    """添加 FAQ，并在同一事务关闭本群规范空白后完全相同的未答问题。"""
    question = _contests._normalize_question(question)
    answer = _conn._as_str(answer)
    if not question or not answer:
        raise ValueError("FAQ 的问题与答案都不能为空")
    with _conn._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO group_faq (group_id, question, answer, source, status, updated_at)"
            " VALUES (?, ?, ?, ?, 'confirmed', ?)",
            (group_id, question, answer, _conn._as_str(source or ""), _conn.now_str()),
        )
        fid = cur.lastrowid
        _close_same_question(conn, group_id, question, fid)
        _admin.add_audit(operator, group_id, "create", "faq", str(fid), "", question[:50], conn=conn)
        return fid


def list_faq(group_id: str, keyword: str | None = None,
             status: str | None = None) -> list[dict]:
    """列出本群 FAQ。

    ``status`` 语义（默认口径偏安全，避免未确认草稿被当作正式答案）：
      - ``None``（默认）：只返回 confirmed —— 问答检索、上下文注入一律只认已生效条目；
      - ``"all"``：除 rejected/deleted 外的全部，供管理端总览；
      - 具体状态：``pending`` / ``confirmed`` / ``rejected`` / ``deleted`` 精确筛选。
    """
    where = ["group_id = ?"]
    args: list = [group_id]
    if status is None:
        where.append("status = 'confirmed'")
    elif status == "all":
        where.append("status NOT IN ('rejected', 'deleted')")
    else:
        where.append("status = ?")
        args.append(_conn._as_str(status))
    if keyword:
        where.append("(question LIKE ? OR answer LIKE ?)")
        args.extend([f"%{keyword}%", f"%{keyword}%"])
    with _conn._connect() as conn:
        rows = conn.execute(
            f"SELECT {_FAQ_COLS} FROM group_faq WHERE {' AND '.join(where)} ORDER BY id DESC",
            args,
        ).fetchall()
    return [_row_to_faq(r) for r in rows]


def get_faq(group_id: str, faq_id: int) -> dict | None:
    """只读取本群仍有效（confirmed）的 FAQ，供等待模型后的复核使用。"""
    with _conn._connect() as conn:
        row = conn.execute(
            f"SELECT {_FAQ_COLS} FROM group_faq"
            " WHERE group_id=? AND id=? AND status = 'confirmed'",
            (group_id, faq_id),
        ).fetchone()
    return _row_to_faq(row) if row else None


def add_faq_pending(group_id: str, question: str, answer: str, source: str = "",
                    operator: str = "") -> dict:
    """写入待确认 FAQ（老师规则通知提炼），不直接生效。

    去重口径：同群同问题已有 pending 草稿 → 覆盖答案（老师又补充了规则细节）；
    已有 confirmed 条目 → 原样保留、不再生成草稿，避免重复打扰。

    Returns:
        {"faq_id": int, "question": str, "answer": str, "status": str, "created": bool}
        ``created=False`` 表示命中了既有条目（pending 已更新 / confirmed 已存在）。
    """
    question = _contests._normalize_question(question)
    answer = _conn._as_str(answer)
    if not question or not answer:
        raise ValueError("FAQ 的问题与答案都不能为空")
    with _conn._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, status, answer FROM group_faq WHERE group_id = ? AND question = ?"
            " AND status IN ('pending', 'confirmed') ORDER BY id DESC",
            (group_id, question),
        ).fetchone()
        if row is not None:
            fid, status, stored_answer = int(row[0]), row[1], row[2]
            if status == FAQ_PENDING and stored_answer != answer:
                conn.execute(
                    "UPDATE group_faq SET answer = ?, source = ?, updated_at = ? WHERE id = ?",
                    (answer, _conn._as_str(source or ""), _conn.now_str(), fid),
                )
                _admin.add_audit(operator, group_id, "update", "faq_pending", str(fid),
                                 str(stored_answer)[:50], answer[:50], conn=conn)
            return {"faq_id": fid, "question": question, "answer": answer,
                    "status": status, "created": False}
        cur = conn.execute(
            "INSERT INTO group_faq (group_id, question, answer, source, status, updated_at)"
            " VALUES (?, ?, ?, ?, 'pending', ?)",
            (group_id, question, answer, _conn._as_str(source or ""), _conn.now_str()),
        )
        fid = cur.lastrowid
        _admin.add_audit(operator, group_id, "create", "faq_pending", str(fid), "",
                         question[:50], conn=conn)
        return {"faq_id": fid, "question": question, "answer": answer,
                "status": FAQ_PENDING, "created": True}


def get_pending_faq(group_id: str, faq_id: int) -> dict | None:
    """按编号读取本群某条 pending 草稿。"""
    try:
        key = int(faq_id)
    except (TypeError, ValueError):
        return None
    with _conn._connect() as conn:
        row = conn.execute(
            f"SELECT {_FAQ_COLS} FROM group_faq"
            " WHERE group_id = ? AND id = ? AND status = 'pending'",
            (group_id, key),
        ).fetchone()
    return _row_to_faq(row) if row else None


def get_faq_record(faq_id: int) -> dict | None:
    """按编号读取任意状态、任意群的 FAQ（私聊确认时据此定位归属群）。"""
    try:
        key = int(faq_id)
    except (TypeError, ValueError):
        return None
    with _conn._connect() as conn:
        row = conn.execute(
            f"SELECT {_FAQ_COLS} FROM group_faq WHERE id = ?", (key,),
        ).fetchone()
    return _row_to_faq(row) if row else None


def list_pending_faq(group_id: str = "", owner: str = "") -> list[dict]:
    """列出待确认 FAQ。

    Args:
        group_id: 空表示全部群（管理端私聊/「确认全部」用）。
        owner: 空表示不限提交人；填老师标识时只返回该老师提炼的草稿。
    """
    where, args = ["status = 'pending'"], []
    if _conn._as_str(group_id):
        where.insert(0, "group_id = ?")
        args.append(_conn._as_str(group_id))
    if _conn._as_str(owner):
        where.append("source = ?")
        args.append(teacher_source(owner))
    with _conn._connect() as conn:
        rows = conn.execute(
            f"SELECT {_FAQ_COLS} FROM group_faq WHERE {' AND '.join(where)} ORDER BY id DESC",
            args,
        ).fetchall()
    return [_row_to_faq(r) for r in rows]


def confirm_faq(group_id: str, faq_id: int, operator: str = "") -> dict | None:
    """把 pending 草稿转为 confirmed 生效，并关闭本群同题未答问题。"""
    try:
        key = int(faq_id)
    except (TypeError, ValueError):
        return None
    with _conn._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"SELECT {_FAQ_COLS} FROM group_faq WHERE id = ? AND group_id = ? AND status = 'pending'",
            (key, group_id),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE group_faq SET status = 'confirmed', updated_at = ? WHERE id = ?",
            (_conn.now_str(), key),
        )
        _close_same_question(conn, group_id, row[2], key)
        _admin.add_audit(operator, group_id, "confirm", "faq", str(key), "pending",
                         "confirmed", conn=conn)
        record = _row_to_faq(row)
    record["status"] = FAQ_CONFIRMED
    return record


def reject_faq(group_id: str, faq_id: int, operator: str = "", note: str = "") -> bool:
    """驳回 pending 草稿：置为 rejected，保留审计但不参与召回。"""
    try:
        key = int(faq_id)
    except (TypeError, ValueError):
        return False
    with _conn._connect() as conn:
        cur = conn.execute(
            "UPDATE group_faq SET status = 'rejected', updated_at = ?"
            " WHERE id = ? AND group_id = ? AND status = 'pending'",
            (_conn.now_str(), key, group_id),
        )
        if cur.rowcount:
            _admin.add_audit(operator, group_id, "reject", "faq", str(key), "pending",
                             ("rejected " + (note or "")).strip(), conn=conn)
        return cur.rowcount > 0


def delete_faq(group_id: str, faq_id: int, operator: str = "") -> bool:
    """标记 FAQ 删除（保留审计，不物理删除）。"""
    with _conn._connect() as conn:
        cur = conn.execute(
            "UPDATE group_faq SET status = 'deleted', updated_at = ? WHERE id = ? AND group_id = ?",
            (_conn.now_str(), faq_id, group_id),
        )
        if cur.rowcount:
            _admin.add_audit(operator, group_id, "delete", "faq", str(faq_id), "",
                      "deleted", conn=conn)
        return cur.rowcount > 0


_UNANSWERED_COLS = "id, group_id, question, asker, asked_at, status, faq_id"


def _row_to_unanswered(r: sqlite3.Row) -> dict:
    return {
        "id": r[0], "group_id": r[1], "question": r[2], "asker": r[3],
        "asked_at": r[4], "status": r[5], "faq_id": r[6],
    }


def add_unanswered_question(group_id: str, question: str, asker: str = "") -> int:
    """记录未答问题；已有语义相同的 open 问题则复用编号，避免重复堆积。

    口语环境下同一意图会有很多说法（「这周有周赛吗」「周赛有没」），
    只按文本完全一致去重会把它们记成多条，编号迅速膨胀。因此先用
    关键词/相似度做一次模糊合并。
    """
    question = _contests._normalize_question(question)
    if not question:
        raise ValueError("问题不能为空")
    with _conn._connect() as conn:
        rows = conn.execute(
            "SELECT id, question FROM unanswered_questions"
            " WHERE group_id = ? AND status = 'open'",
            (group_id,),
        ).fetchall()
        threshold = config.QA_KB_MATCH_MIN_SCORE
        for qid, stored in rows:
            if _contests._normalize_question(stored) == question:
                return qid
            if content_filter.same_intent(question, stored):
                return qid
            if content_filter.question_match_score(question, stored) >= threshold:
                return qid
        cur = conn.execute(
            "INSERT INTO unanswered_questions (group_id, question, asker, asked_at, status)"
            " VALUES (?, ?, ?, ?, 'open')",
            (group_id, question, _conn._as_str(asker or ""), _conn.now_str()),
        )
        return cur.lastrowid


def list_unanswered(group_id: str, status: str = "open") -> list[dict]:
    with _conn._connect() as conn:
        rows = conn.execute(
            f"SELECT {_UNANSWERED_COLS} FROM unanswered_questions"
            " WHERE group_id = ? AND status = ? ORDER BY id DESC",
            (group_id, status),
        ).fetchall()
    return [_row_to_unanswered(r) for r in rows]


def answer_unanswered_question(group_id: str, qid: int, answer: str,
                               operator: str = "") -> dict | None:
    """把某条未答问题直接沉淀为 FAQ 并标记已答（单事务）。

    这就是「老师按编号回答」的落库逻辑：写入 group_faq、把该问题置为
    answered 并回填 faq_id，同时顺带关闭本群其它同题未答问题。

    Returns:
        {"faq_id": int, "question": str, "asker": str}；问题不存在、
        不属于本群或已处理时返回 None。
    """
    answer = _conn._as_str(answer)
    if not answer:
        raise ValueError("答案不能为空")
    with _conn._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT question, asker FROM unanswered_questions"
            " WHERE id = ? AND group_id = ? AND status = 'open'",
            (qid, group_id),
        ).fetchone()
        if row is None:
            return None
        question, asker = row[0], row[1]
        cur = conn.execute(
            "INSERT INTO group_faq (group_id, question, answer, source, status, updated_at)"
            " VALUES (?, ?, ?, ?, 'confirmed', ?)",
            (group_id, question, answer, _conn._as_str(operator or "老师回答"), _conn.now_str()),
        )
        fid = cur.lastrowid
        conn.execute(
            "UPDATE unanswered_questions SET status = 'answered', faq_id = ? WHERE id = ?",
            (fid, qid),
        )
        _close_same_question(conn, group_id, question, fid, skip_qid=qid)
        _admin.add_audit(operator, group_id, "answer_question", "unanswered_question",
                  str(qid), "", question, conn=conn)
        return {"faq_id": fid, "question": question, "asker": asker}


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
    group_id = _conn._as_str(group_id)
    title = _conn._as_str(title)
    if not title:
        raise ValueError("征集标题不能为空")
    fields = fields or []
    if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
        raise ValueError("登记字段必须是字符串列表")
    deadline = _conn._clean_time(deadline)
    capacity = _conn._as_int(capacity)
    with _conn._connect() as conn:
        cur = conn.execute(
            "INSERT INTO collections (group_id, title, purpose, deadline, capacity,"
            " created_by, status, fields_json, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                group_id, title, _conn._as_str(purpose or ""), deadline, capacity,
                _conn._as_str(operator or ""), "open",
                json.dumps(fields, ensure_ascii=False), _conn.now_str(), _conn.now_str(),
            ),
        )
        _admin.add_audit(operator, group_id, "create", "collection", str(cur.lastrowid), "",
                  title, conn=conn)
        return cur.lastrowid


def list_collections(group_id: str, status: str | None = None) -> list[dict]:
    with _conn._connect() as conn:
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
    with _conn._connect() as conn:
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
    with _conn._connect() as conn:
        row = conn.execute(
            f"SELECT {_COLLECTION_COLS} FROM collections WHERE id = ?", (cid,)
        ).fetchone()
    return _row_to_collection(row) if row else None


def set_collection_status(cid: int, status: str, operator: str = "") -> bool:
    """关闭/恢复征集（status ∈ open/closed）。"""
    if status not in ("open", "closed"):
        raise ValueError(f"征集状态非法：{status}")
    with _conn._connect() as conn:
        cur = conn.execute(
            "UPDATE collections SET status = ?, updated_at = ? WHERE id = ?",
            (status, _conn.now_str(), cid),
        )
        if cur.rowcount == 0:
            return False
        _admin.add_audit(operator, "", "set_status", "collection", str(cid),
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

    with _conn._connect() as conn:
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
            deadline = _training._delivery_parse_time(coll["deadline"])
            if deadline is None:
                return False, "征集截止时间无效，请联系老师"
            if deadline <= _training._delivery_parse_time(_conn.now_str()):
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
        timestamp = _conn.now_str()
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
    with _conn._connect() as conn:
        cur = conn.execute(
            "UPDATE collection_responses SET status = 'cancelled', updated_at = ?"
            " WHERE collection_id = ? AND member_id = ? AND status = 'registered'",
            (_conn.now_str(), cid, member_id),
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
    with _conn._connect() as conn:
        row = conn.execute(
            f"SELECT {_COLLECTION_RESPONSE_COLS} FROM collection_responses"
            " WHERE collection_id = ? AND member_id = ? AND status = 'registered'",
            (cid, member_id),
        ).fetchone()
    return _row_to_collection_response(row) if row else None


def list_collection_responses(cid: int) -> list[dict]:
    """管理员明细：某征集的全部 registered 报名。"""
    with _conn._connect() as conn:
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
    with _conn._connect() as conn:
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
