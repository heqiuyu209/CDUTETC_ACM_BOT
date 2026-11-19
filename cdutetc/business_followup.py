"""业务消息的可信引用与地点补充：只改当前群的明确对象，不调用模型。"""

import json
import re
from contextlib import closing
from datetime import datetime, timedelta

from cdutetc import config, database as db

_LOCATION = re.compile(r"^(?P<title>.*?)\s*(?:地点|教室)\s*(?:改成|改到|改为|换到|是|在|为|[：:,，])?\s*(?P<location>[^\n]{1,40})[。！!]?$")
_LOCATION_MESSAGE = re.compile(r"^.*?(?:地点|教室)\s*\S[\s\S]*$", re.DOTALL)
_TARGET = re.compile(r"^(课程|集训|比赛)\s*#?([1-9]\d*)\s*$")
_UNSURE = re.compile(
    r"[?？]|[吗呢吧]\s*[。！!]*$|哪里|哪儿|哪间|在哪|待定|暂定|未定|"
    r"不确定|未确定|没确定|没定|不一定|或许|估计|也许|可能|大概|应该|不知道|是不是|不是|不在|"
    r"别改|不要|不用|不改|取消|撤销|推迟|能不能|是否|还是|或者|听说|据说|我猜|似乎"
)
_GENERIC_TITLES = {"", "补充一下", "补充", "改一下", "今天", "今晚"}
_LOCATION_QUERY = re.compile(r"哪里|哪儿|哪间|在哪|什么地点|地点(?:是什么|是啥)|教室(?:是哪|是什么)")


def _now(now=None):
    return db._delivery_now(now)


def _location_match(text):
    plain = (text or "").strip()
    match = _LOCATION.fullmatch(plain)
    if match is None or re.search(r"\d{1,2}[:：点]", plain):
        return None
    return match


def _location_parts(text):
    match = _location_match(text)
    if match is None or _UNSURE.search(text):
        return None
    location = match["location"].strip(" ，,。！!")
    if (not location or location in {"哪", "哪呢", "什么", "吗"}
            or re.search(r"时间|几点|明天|后天|\d{1,2}:\d{2}|http|<|>|\[|"
                         r"[,，;；。！!]|从.*(?:改|换)|改成|改到|改为|换到", location)):
        return None
    return match["title"].strip(" ，,。"), location


def is_location_message(text):
    plain = (text or "").strip()
    # 识别入口比可写槽位宽：多行、长值和不确定语句只能确认，不能漏回模型写库。
    return bool(_LOCATION_MESSAGE.fullmatch(plain)
                and (_UNSURE.search(plain) or not re.search(r"\d{1,2}[:：点]", plain)))


def is_location_query(text):
    plain = (text or "").strip()
    if re.search(r"报名|登记|注册|链接|网址|官网", plain) and not re.search(r"地点|教室", plain):
        return False
    return bool(re.search(r"地点|教室|训练|集训|课程|比赛|校赛|周赛|上课", plain)
                and _LOCATION_QUERY.search(plain))


def _allowed(conn, gid, sender):
    if gid not in config.ALLOWED_GROUPS:
        return False
    if sender in config.SUPER_ADMIN:
        return True
    if sender not in config.TEACHER_WHITELIST:
        return False
    owners = {row[0] for row in conn.execute("SELECT user_id FROM group_teacher_assignments WHERE group_id=?", (gid,))}
    return sender in owners if owners else sender in config.TEACHER_WHITELIST


def _record(conn, gid, kind, oid):
    table = {"training_session": "training_sessions", "competition": "competitions"}.get(kind)
    if not table:
        return None
    cur = conn.execute(f"SELECT * FROM {table} WHERE id=?", (oid,))
    row = cur.fetchone()
    if row is None:
        return None
    item = dict(zip((col[0] for col in cur.description), row))
    if item["status"] != "confirmed":
        return None
    if kind == "training_session":
        if item["group_id"] != gid:
            return None
    else:
        if gid not in db._competition_target_groups_on(conn, oid):
            return None
        loc = conn.execute("SELECT location FROM group_competition_locations WHERE group_id=? AND competition_id=?", (gid, oid)).fetchone()
        item["location"] = loc[0] if loc else ""
    return item


def _stamp(value):
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _candidate(item, kind, occurrence):
    return {"kind": kind, "id": item["id"], "snapshot": item, "occurrence": _stamp(occurrence)}


def _valid(candidate, current):
    from cdutetc import qa_context

    item = candidate["snapshot"]
    occurrence = db._delivery_parse_time(candidate["occurrence"])
    if occurrence is None:
        return False
    if candidate["kind"] == "competition":
        return occurrence > current and db._delivery_parse_time(item["official_time"]) == occurrence
    if not qa_context._valid_session_occurrence(item, occurrence):
        return False
    _, end_clock = qa_context._normalized_clocks(item.get("start_time"), item.get("end_time"))
    end = occurrence.replace(hour=end_clock.hour, minute=end_clock.minute, second=end_clock.second,
                             microsecond=0) if end_clock else occurrence
    if end < occurrence:
        end += timedelta(days=1)
    return end > current


def remember_digest(group_id, message_id, detail, now=None):
    """只由成功发送入口调用：保存本群自己发出的消息到带类型业务快照。"""
    gid, current = str(group_id), _now(now)
    if gid not in config.ALLOWED_GROUPS or not isinstance(message_id, str) or not 1 <= len(message_id) <= 256:
        return
    items = []
    with closing(db._connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        for key, kind in (("sessions", "training_session"), ("competitions", "competition")):
            for shown in detail.get(key) or []:
                item = _record(conn, gid, kind, shown.get("id"))
                occurrence = shown.get("start_dt")
                if item is None or not isinstance(occurrence, datetime):
                    continue
                occurrence = _now(occurrence)
                # 生成早报之后对象已修改时，不把新对象冒认成刚发出的旧内容。
                title_key = "title" if kind == "training_session" else "name"
                if item[title_key] != shown.get(title_key):
                    continue
                if kind == "training_session" and ((item.get("location") or "") != (shown.get("location") or "")
                        or not db.training_occurrence_valid(item, occurrence)):
                    continue
                if kind == "training_session":
                    end_clock = db._training_start_clock(item.get("end_time"))
                    end_text = end_clock.strftime("%H:%M") if end_clock else ""
                    if (end_text != (shown.get("end_text") or "")
                            or (item.get("requirement") or "").strip() != (shown.get("requirement") or "").strip()):
                        continue
                if kind == "competition" and db._delivery_parse_time(item["official_time"]) != occurrence:
                    continue
                if kind == "competition" and ((item.get("location") or "").strip() != (shown.get("location") or "").strip()
                        or (item.get("summary") or "").strip() != (shown.get("summary") or "").strip()
                        or (item.get("official_site") or "").strip() != (shown.get("site") or shown.get("url") or "").strip()):
                    continue
                candidate = _candidate(item, kind, occurrence)
                if _valid(candidate, current):
                    items.append(candidate)
        expires = current.replace(hour=23, minute=59, second=59, microsecond=0)
        conn.execute("DELETE FROM business_message_context WHERE expires_at<?", (_stamp(current),))
        conn.execute("DELETE FROM business_patch_receipts WHERE created_at<?", (_stamp(current - timedelta(days=7)),))
        conn.execute("INSERT OR IGNORE INTO business_message_context(group_id,message_id,items_json,expires_at)"
                     " VALUES (?,?,?,?)", (gid, message_id, json.dumps(items, ensure_ascii=False, sort_keys=True), _stamp(expires)))


def _today_candidates(conn, gid, current, title):
    from cdutetc import qa_context

    out = []
    # 无引用、无明确标题时仅考虑今天，不能把“地点”套到下周另一场安排。
    days = 1 if title in _GENERIC_TITLES else 7
    # 业务选择不能沿用模型上下文的14条裁剪；明确编号和第15项也应可定位。
    for row in conn.execute("SELECT id FROM training_sessions WHERE group_id=? AND status='confirmed'", (gid,)):
        item = _record(conn, gid, "training_session", row[0])
        if item is None:
            continue
        clock, _ = qa_context._normalized_clocks(item.get("start_time"), item.get("end_time"))
        if clock is None:
            continue
        for offset in range(days):
            occurrence = current.replace(hour=clock.hour, minute=clock.minute, second=clock.second,
                                         microsecond=0) + timedelta(days=offset)
            candidate = _candidate(item, "training_session", occurrence)
            if _valid(candidate, current):
                out.append(candidate)
    for row in conn.execute("SELECT id FROM competitions WHERE status='confirmed'"):
        item = _record(conn, gid, "competition", row[0])
        if item:
            start = db._delivery_parse_time(item["official_time"])
            if start and current <= start < current.replace(hour=0, minute=0, second=0) + timedelta(days=days):
                out.append(_candidate(item, "competition", start))
    return out


def _select(items, title):
    typed = _TARGET.fullmatch(title)
    if typed:
        kind = "competition" if typed[1] == "比赛" else "training_session"
        return [item for item in items if item["kind"] == kind and item["id"] == int(typed[2])]
    if title in _GENERIC_TITLES:
        return items
    return [item for item in items if item["snapshot"].get("title", item["snapshot"].get("name", "")) == title]


def handle_location(text, group_id, sender_id, msg_id="", reference_id="", now=None):
    """明确地点补充优先于聊天；未知或多个目标时只问对象，不追问已登记时间。"""
    parts = _location_parts(text)
    if parts is None and not is_location_message(text):
        return None
    gid, sender, current = str(group_id), str(sender_id), _now(now)
    if gid not in config.ALLOWED_GROUPS:
        return {"reply": "本群未授权。", "changed": False}
    with closing(db._connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if not _allowed(conn, gid, sender):
            return {"reply": "地点补充需要本群负责老师或超管确认。", "changed": False}
        if parts is None:
            return {"reply": "地点还不够明确，请老师确认后用「课程或比赛名称 地点 新地点」补充；本次未修改安排。", "changed": False}
        title, location = parts
        if msg_id:
            receipt = conn.execute("SELECT sender_id,reply FROM business_patch_receipts WHERE group_id=? AND message_id=?", (gid, msg_id)).fetchone()
            if receipt:
                return {"reply": receipt[1] if receipt[0] == sender else "该消息已处理。", "changed": False}
        if reference_id:
            row = conn.execute("SELECT items_json,expires_at FROM business_message_context WHERE group_id=? AND message_id=?", (gid, reference_id)).fetchone()
            if row is None or row[1] < _stamp(current):
                return {"reply": "这条引用未关联到本群当前安排，请写明课程或比赛名称、编号。", "changed": False}
            items = json.loads(row[0])
        else:
            items = _today_candidates(conn, gid, current, title)
        selected = _select(items, title)
        if len(selected) != 1:
            labels = [f"{'课程' if item['kind']=='training_session' else '比赛'} {item['id']}："
                      f"{item['snapshot'].get('title') or item['snapshot'].get('name')}" for item in selected[:5]]
            return {"reply": "请确认要补充哪项安排的地点。" + ("\n" + "\n".join(labels) if labels else "")
                    + "\n例如：课程 1 地点 93501。", "changed": False}
        candidate = selected[0]
        kind, oid = candidate["kind"], candidate["id"]
        latest = _record(conn, gid, kind, oid)
        if latest != candidate["snapshot"] or not _valid(candidate, current):
            return {"reply": "这项安排已变更、取消或结束，请明确当前课程或比赛名称、编号后再确认地点。", "changed": False}
        if kind == "training_session":
            conn.execute("UPDATE training_sessions SET location=?,updated_at=? WHERE id=? AND group_id=? AND status='confirmed'",
                         (location, _stamp(current), oid, gid))
        else:
            conn.execute("INSERT INTO group_competition_locations(group_id,competition_id,location,updated_by,updated_at)"
                         " VALUES (?,?,?,?,?) ON CONFLICT(group_id,competition_id) DO UPDATE"
                         " SET location=excluded.location,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                         (gid, oid, location, sender, _stamp(current)))
        name = latest.get("title") or latest.get("name")
        when = candidate["occurrence"][:16]
        if kind == "training_session":
            from cdutetc import qa_context

            _, end_clock = qa_context._normalized_clocks(latest.get("start_time"), latest.get("end_time"))
            if end_clock:
                when += "–" + end_clock.strftime("%H:%M")
        reply = f"已更新本群「{name}」地点：{location}。时间仍为 {when}。"
        db.add_audit(sender, gid, "location_followup", kind, str(oid), latest.get("location", ""), location, conn=conn)
        if msg_id:
            conn.execute("INSERT INTO business_patch_receipts(group_id,message_id,sender_id,reply,created_at) VALUES (?,?,?,?,?)",
                         (gid, msg_id, sender, reply, _stamp(current)))
        return {"reply": reply, "changed": True}


def set_competition_location(group_id, sender_id, competition_id, location, now=None, *, conn=None):
    """结构化老师意图沿用本群地点覆盖，保持共享比赛资料完整。"""
    gid, sender, current = str(group_id), str(sender_id), _now(now)
    if not isinstance(location, str) or not 1 <= len(location.strip()) <= 40:
        return False
    def write(connection):
        if not _allowed(connection, gid, sender):
            return False
        item = _record(connection, gid, "competition", competition_id)
        if item is None:
            return False
        connection.execute("INSERT INTO group_competition_locations(group_id,competition_id,location,updated_by,updated_at)"
                     " VALUES (?,?,?,?,?) ON CONFLICT(group_id,competition_id) DO UPDATE"
                     " SET location=excluded.location,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                     (gid, competition_id, location.strip(), sender, _stamp(current)))
        db.add_audit(sender, gid, "location_followup", "competition", str(competition_id), item.get("location", ""), location, conn=connection)
        return True
    if conn is not None:
        return write(conn)
    with closing(db._connect()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        return write(connection)
