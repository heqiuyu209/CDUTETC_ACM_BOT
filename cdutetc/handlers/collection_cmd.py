"""征集命令：/征集创建、/征集报名、/征集汇总、/征集截止。"""

import logging
import re
from cdutetc import database as db
from cdutetc.utils import can_manage_group

from cdutetc.handlers import _shared as _shared

logger = logging.getLogger(__name__)


def cmd_collection_create(group_id: str, sender: str, args: str) -> list[dict]:
    """老师/超管发起征集。格式：/征集 <标题> [截止时间] [名额] [字段1,字段2] [用途=xxx]"""
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可发起征集"}]
    parts = args.strip().split(None, 1)
    if not parts:
        return [{"group_id": group_id, "text": "用法：/征集 <标题> [截止时间] [名额] [字段1,字段2]\n例如：/征集 机房设备借用 2026-11-01 20:00 5 设备,时段"}]
    title = parts[0]
    rest = parts[1].strip() if len(parts) > 1 else ""
    deadline = ""
    capacity = None
    fields: list[str] = []
    purpose = ""
    if rest:
        m = re.match(r"^(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})\b", rest)
        if m:
            deadline = f"{m.group(1)} {m.group(2)}"
            rest = rest[m.end():].strip()
        else:
            m = re.match(r"^(\d{4}-\d{2}-\d{2})\b", rest)
            if m:
                deadline = m.group(1)
                rest = rest[m.end():].strip()
        for tok in rest.split():
            if tok.startswith("用途="):
                purpose = tok.split("=", 1)[1].strip()
            elif capacity is None and tok.isdigit():
                capacity = int(tok)
            else:
                fields.extend(f.strip() for f in tok.replace("，", ",").split(",") if f.strip())
    try:
        cid = db.create_collection(group_id, title, purpose=purpose, deadline=deadline,
                                   capacity=capacity, fields=fields, operator=sender)
    except ValueError as e:
        return [{"group_id": group_id, "text": str(e)}]
    return [{"group_id": group_id, "text": f"已发起征集 #{cid}：{title}（用 /报名 {cid} ... 报名，/征集汇总 {cid} 查看）"}]


def cmd_collection_register(group_id: str, sender: str, args: str) -> list[dict]:
    """成员报名/更新/取消自己的征集登记，按人+征集幂等。"""
    parts = args.strip().split(None, 1)
    if not parts:
        return [{"group_id": group_id, "text": "用法：/报名 <征集id> [字段=值 ...]\n取消：/报名 取消 <征集id>"}]
    if parts[0] == "取消":
        rest = parts[1].strip().split(None, 1) if len(parts) > 1 else []
        if not rest:
            return [{"group_id": group_id, "text": "用法：/报名 取消 <征集id>"}]
        try:
            cid = int(rest[0])
        except ValueError:
            return [{"group_id": group_id, "text": "征集 id 应为数字"}]
        _, msg = db.cancel_collection_registration(cid, group_id, sender)
        return [{"group_id": group_id, "text": msg}]
    try:
        cid = int(parts[0])
    except ValueError:
        return [{"group_id": group_id, "text": "征集 id 应为数字"}]
    data = None
    if len(parts) > 1:
        try:
            data = _shared._parse_field_values(parts[1])
        except ValueError as e:
            return [{"group_id": group_id, "text": f"报名参数错误：{e}"}]
        if not data:
            return [{"group_id": group_id, "text": "请使用 字段=值 填写报名资料"}]
    _, msg = db.register_collection(cid, group_id, sender, data)
    return [{"group_id": group_id, "text": msg}]


def cmd_collection_summary(group_id: str, sender: str, args: str) -> list[dict]:
    """群内只展示汇总；请求者是管理员也不向群成员发送个人明细。"""
    try:
        cid = int(args.strip().split()[0])
    except (ValueError, IndexError):
        return [{"group_id": group_id, "text": "用法：/征集汇总 <征集id>"}]
    coll = db.get_collection(cid)
    if coll is None or coll["group_id"] != group_id:
        return [{"group_id": group_id, "text": "征集不存在或不属于本群"}]
    s = db.summarize_collection(cid)
    remaining = "不限" if s["remaining"] is None else str(s["remaining"])
    lines = [
        f"征集：{s['title']}（{s['status']}）",
        f"用途：{s['purpose'] or '-'}",
        f"截止：{coll['deadline'] or '不限'}",
        f"已报名：{s['registered']} 人",
        f"已取消：{s['cancelled']} 人",
        f"剩余名额：{remaining}",
    ]
    return [{"group_id": group_id, "text": "\n".join(lines)}]


def cmd_collection_close(group_id: str, sender: str, args: str) -> list[dict]:
    """老师/超管关闭征集，停止报名。"""
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可关闭征集"}]
    try:
        cid = int(args.strip().split()[0])
    except (ValueError, IndexError):
        return [{"group_id": group_id, "text": "用法：/征集关闭 <征集id>"}]
    coll = db.get_collection(cid)
    if coll is None or coll["group_id"] != group_id:
        return [{"group_id": group_id, "text": "征集不存在或不属于本群"}]
    ok = db.set_collection_status(cid, "closed", sender)
    return [{"group_id": group_id, "text": "已关闭征集，停止报名" if ok else "操作失败"}]
