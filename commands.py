"""命令处理模块（v2）：命令式操作 + 权限校验 + 全部业务命令。

对应 PRODUCT_SPEC v2：
- 移除订阅/取消订阅/比赛列表/比赛详情、审核列表/通过/拒绝、活动全套命令；
  新增 /修正（超管）、/征集 /征集关闭（老师/超管）、/征集汇总 /报名（群成员）；
  发布改为免审（老师白名单，见 main.py）。
- 命令式敏感操作 + 清晰帮助：所有写操作必须以命令形式显式发出，禁止隐式触发。
- 权限默认全拒绝：super_admin=超管（/修正、全局管理）；teacher=老师
  （课程/FAQ、征集管理）；member=普通成员只可报名/查询。
- 跨群隔离：命令处理始终携带 group_id / sender_id，数据库层按群过滤。
- 返回值统一为消息列表：[{"group_id": str, "text": str}, ...]，
  由上层（main.py）负责实际发送，本模块不接触任何 QQ SDK / 日志冒充发送。
"""

import asyncio
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import config
import crawler
import database as db
import llm
from fetcher import FetchError, fetch_text
from utils import can_manage_group, is_super_admin, parse_command

TZ = ZoneInfo("Asia/Shanghai")

HELP_TEXT = """【CDUTETC ACM 事务助手 · 命令帮助】
--- 比赛资料 ---
发布 <官网链接>          免审发布比赛并发送群通知（老师，@机器人发送）
/修正 <比赛id> <字段=值 ...>  修正已发布比赛字段（超管）
  例：/修正 1 time=2026-12-01 19:00 name="ICPC 2026 区域赛"
/修正 <比赛id> 重新提取 <链接>  重新提取官网信息（超管）
--- 学校安排 ---
/校安排                  查看学校统一安排（仅共享部分）
--- 课程与训练 ---
/课程列表                查看本群课程安排
/课程添加 <标题> [星期几] [开始-结束] [地点]  添加课程（老师/超管）
/课程取消 <id>           取消课程（老师/超管）
--- 征集登记 ---
/征集 <标题> [截止时间] [名额] [字段1,字段2]  发起征集（老师/超管）
/报名 <征集id> [字段=值 ...]   报名/更新我的登记（群成员）
/报名 取消 <征集id>      取消我的报名（群成员）
/征集汇总 <征集id>       查看本群人数和剩余名额（群成员）
/征集关闭 <征集id>       关闭征集停止报名（老师/超管）
--- FAQ 与提问 ---
/FAQ列表 [关键词]       查看本群 FAQ
/FAQ添加 <问题> = <答案> 添加 FAQ 并关闭同群同题未答问题（老师/超管）
/FAQ删除 <id>            删除 FAQ（老师/超管）
/提问 <问题>             记录未答问题，老师稍后回答
/问题列表                查看未答问题（老师/超管）
--- 提醒与抓取 ---
/提醒状态                查看提醒投递队列状态（老师/超管）
/抓取状态                查看公共来源抓取状态（老师/超管）
/抓取一次                立即抓取一次公共来源（老师/超管）
--- 其他 ---
/帮助                    显示本帮助"""# ---------------------------------------------------------------------
# v2 命令：修正（超管）
# ---------------------------------------------------------------------

_AMEND_FIELD_MAP = {
    "name": "name",
    "edition": "edition",
    "site": "official_site",
    "official_site": "official_site",
    "reg_start": "official_reg_start",
    "official_reg_start": "official_reg_start",
    "reg_deadline": "official_reg_deadline",
    "official_reg_deadline": "official_reg_deadline",
    "time": "official_time",
    "official_time": "official_time",
    "tracks": "tracks",
    "rules": "official_rules",
    "official_rules": "official_rules",
    "device_req": "device_req",
    "summary": "summary",
}

_AMEND_ASSIGNMENT = re.compile(r"([^\s=,，]+)\s*=")
_AMEND_NEXT_FIELD = re.compile(r"(?:\s+|[,，]\s*)(?=[^\s=,，]+\s*=)")
_AMEND_QUOTES = {'"': '"', "'": "'", "“": "”", "‘": "’"}


def _parse_field_values(text: str, field_map: dict[str, str] | None = None) -> dict[str, str]:
    """读取完整字段值；引号内的赋值片段、逗号和空格均属于正文。"""
    fields: dict[str, str] = {}
    pos = 0
    while pos < len(text):
        while pos < len(text) and (text[pos].isspace() or text[pos] in ",，"):
            pos += 1
        if pos == len(text):
            break
        assignment = _AMEND_ASSIGNMENT.match(text, pos)
        if assignment is None:
            raise ValueError("参数应为 字段=值；带赋值片段的文本请用引号包裹")
        key = assignment[1]
        field = field_map.get(key) if field_map is not None else key
        if field is None:
            raise ValueError(f"不支持的字段：{key}")
        if field in fields:
            raise ValueError(f"字段重复：{field}（含别名）")
        pos = assignment.end()
        value_start = pos
        quote_start = pos
        while quote_start < len(text) and text[quote_start].isspace():
            quote_start += 1
        closing_quote = _AMEND_QUOTES.get(text[quote_start]) if quote_start < len(text) else None
        if closing_quote:
            pos = quote_start + 1
            value: list[str] = []
            while pos < len(text):
                char = text[pos]
                if char == closing_quote:
                    pos += 1
                    break
                if char == "\\" and pos + 1 < len(text) and text[pos + 1] in (closing_quote, "\\"):
                    pos += 1
                    char = text[pos]
                value.append(char)
                pos += 1
            else:
                raise ValueError(f"字段 {key} 的引号未闭合")
            if pos < len(text) and not (text[pos].isspace() or text[pos] in ",，"):
                raise ValueError(f"字段 {key} 的引号后应为空白或逗号分隔的下一个字段")
            fields[field] = "".join(value)
        else:
            separator = _AMEND_NEXT_FIELD.search(text, value_start)
            pos = separator.start() if separator else len(text)
            fields[field] = text[value_start:pos].strip()
    return fields


def _parse_amend_fields(text: str) -> dict[str, str]:
    return _parse_field_values(text, _AMEND_FIELD_MAP)


def _broadcast_to_groups(competition_name: str, cid: int, text: str, exclude: str = "") -> list[dict]:
    """向全部允许群广播一条通知（排除触发群可选）。订阅体系已移除，修正/发布影响全体群。"""
    return [
        {"group_id": gid, "text": text}
        for gid in sorted(config.ALLOWED_GROUPS)
        if gid != exclude
    ]


async def cmd_amend(group_id: str, sender: str, args: str) -> list[dict]:
    """超管修正已发布比赛：字段纠正或重新提取官网，生成修正版本并通知各群。

    用法：
      /修正 <比赛id> <字段=值 ...>      纠正指定字段
      /修正 <比赛id> 重新提取 <链接>    重新抓取官网并提取，覆盖官方字段
    """
    if not is_super_admin(sender):
        return [{"group_id": group_id, "text": "仅超管可修正比赛信息"}]
    parts = args.strip().split(None, 1)
    if not parts:
        return [{"group_id": group_id, "text": "用法：/修正 <比赛id> <字段=值 ...> 或 /修正 <比赛id> 重新提取 <链接>"}]
    try:
        cid = int(parts[0])
    except ValueError:
        return [{"group_id": group_id, "text": "用法：/修正 <比赛id> <字段=值 ...>，id 应为数字"}]
    comp = db.get_competition(cid)
    if comp is None:
        return [{"group_id": group_id, "text": f"比赛 #{cid} 不存在"}]
    rest = parts[1].strip() if len(parts) > 1 else ""
    if rest.startswith("重新提取"):
        return await _amend_refetch(group_id, cid, comp, rest)
    try:
        info = _parse_amend_fields(rest)
    except ValueError as e:
        return [{"group_id": group_id, "text": f"修正参数错误：{e}\n支持：name edition official_site official_reg_start official_reg_deadline official_time tracks official_rules device_req summary（或短名 site/reg_deadline/time/rules 等）"}]
    if not info:
        return [{"group_id": group_id, "text": "请提供至少一个要修正的字段，如 /修正 1 official_reg_deadline=2026-12-01 23:59"}]
    try:
        ok = db.amend_competition(cid, info, operator=sender, note="超管 /修正")
    except ValueError as e:
        return [{"group_id": group_id, "text": f"修正失败：{e}"}]
    if not ok:
        return [{"group_id": group_id, "text": f"比赛 #{cid} 不存在，无法修正"}]
    changed = "；".join(f"{k}={v}" for k, v in info.items())
    msgs = [{"group_id": group_id, "text": f"已修正比赛「{comp['name']}」（#{cid}）：{changed}\n修正已生成版本记录"}]
    msgs += _broadcast_to_groups(comp["name"], cid, f"比赛「{comp['name']}」（#{cid}）已被超管修正：{changed}", exclude=group_id)
    return msgs


async def _amend_refetch(group_id: str, cid: int, comp: dict, rest: str) -> list[dict]:
    """/修正 <id> 重新提取 <链接>：抓官网 → 模型提取 → 严格校验 → 覆盖官方字段。"""
    url = rest[len("重新提取"):].strip()
    if not url:
        return [{"group_id": group_id, "text": "用法：/修正 <比赛id> 重新提取 <链接>"}]
    try:
        page = await fetch_text(url)
    except FetchError as e:
        return [{"group_id": group_id, "text": f"链接抓取失败：{e}"}]
    info, err = await llm.extract_contest(f"链接正文：{page}\n超管修正补充：{rest}")
    if err is not None or not info:
        return [{"group_id": group_id, "text": f"信息提取失败：{err or '未知原因'}"}]
    fields = {
        "name": info.get("name", ""),
        "official_reg_deadline": info.get("reg_deadline", ""),
        "official_time": info.get("contest_time", ""),
        "official_rules": info.get("raw_note", ""),
        "official_site": info.get("source_link", "") or url,
    }
    try:
        ok = db.amend_competition(cid, fields, operator="super_admin", note=f"重新提取：{url}")
    except ValueError as e:
        return [{"group_id": group_id, "text": f"修正失败：{e}"}]
    if not ok:
        return [{"group_id": group_id, "text": f"比赛 #{cid} 不存在，无法修正"}]
    msgs = [{"group_id": group_id, "text": f"已重新提取并修正「{fields['name']}」（#{cid}），修正版本已生成"}]
    msgs += _broadcast_to_groups(fields["name"], cid, f"比赛「{fields['name']}」（#{cid}）官网信息已由超管更新，请以最新信息为准", exclude=group_id)
    return msgs


# ---------------------------------------------------------------------
# v2 命令：征集登记（替代旧活动体系）
# ---------------------------------------------------------------------

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
        ok, msg = db.cancel_collection_registration(cid, group_id, sender)
        return [{"group_id": group_id, "text": msg}]
    try:
        cid = int(parts[0])
    except ValueError:
        return [{"group_id": group_id, "text": "征集 id 应为数字"}]
    data = None
    if len(parts) > 1:
        try:
            data = _parse_field_values(parts[1])
        except ValueError as e:
            return [{"group_id": group_id, "text": f"报名参数错误：{e}"}]
        if not data:
            return [{"group_id": group_id, "text": "请使用 字段=值 填写报名资料"}]
    ok, msg = db.register_collection(cid, group_id, sender, data)
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


def cmd_org_events(group_id: str, sender: str, args: str) -> list[dict]:
    """查看学校统一安排（仅共享给群的 confirmed 安排）。"""
    events = db.list_org_events(status="confirmed", shareable_only=True)
    if not events:
        return [{"group_id": group_id, "text": "当前没有已共享的学校安排"}]
    lines = ["学校统一安排："]
    for e in events:
        t = e["contest_time"] or e["reg_deadline"] or ""
        lines.append(f"#{e['id']} {e['title']}｜{t}｜{e.get('location') or '地点待定'}")
    return [{"group_id": group_id, "text": "\n".join(lines)}]


def cmd_training_list(group_id: str, sender: str, args: str) -> list[dict]:
    sessions = db.list_training_sessions(group_id)
    if not sessions:
        return [{"group_id": group_id, "text": "本群暂无课程安排"}]
    lines = ["本群课程安排："]
    for s in sessions:
        need = "（需带电脑）" if s["needs_laptop"] else ""
        lines.append(
            f"#{s['id']} {s['title']}{need}｜{s['weekday']} {s['start_time']}-{s['end_time']}｜{s['location'] or '地点待定'}"
        )
    return [{"group_id": group_id, "text": "\n".join(lines)}]


def cmd_training_add(group_id: str, sender: str, args: str) -> list[dict]:
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可添加课程"}]
    # 格式：/课程添加 标题 [星期几] [开始-结束] [地点]
    parts = args.strip().split(None, 3)
    if not parts:
        return [{"group_id": group_id, "text": "用法：/课程添加 <标题> [星期几] [开始-结束] [地点]"}]
    title = parts[0]
    weekday = parts[1] if len(parts) > 1 else ""
    time_range = parts[2] if len(parts) > 2 else ""
    location = parts[3] if len(parts) > 3 else ""
    start_time = end_time = ""
    if "-" in time_range:
        st, et = time_range.split("-", 1)
        start_time, end_time = st.strip(), et.strip()
    elif time_range:
        start_time = time_range
    info = {"title": title, "weekday": weekday, "start_time": start_time,
            "end_time": end_time, "location": location}
    tid = db.create_training_session(group_id, info, sender)
    return [{"group_id": group_id, "text": f"已添加课程 #{tid}：{title}"}]


def cmd_training_cancel(group_id: str, sender: str, args: str) -> list[dict]:
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可取消课程"}]
    try:
        tid = int(args.strip().split()[0])
    except (ValueError, IndexError):
        return [{"group_id": group_id, "text": "用法：/课程取消 <id>"}]
    ok = db.cancel_training_session(group_id, tid, sender)
    if not ok:
        return [{"group_id": group_id, "text": f"课程 #{tid} 不存在或不属于本群"}]
    return [{"group_id": group_id, "text": f"已取消课程 #{tid}"}]


def cmd_faq_list(group_id: str, sender: str, args: str) -> list[dict]:
    keyword = args.strip() or None
    faqs = db.list_faq(group_id, keyword)
    if not faqs:
        return [{"group_id": group_id, "text": "本群暂无 FAQ" + ("（含关键词）" if keyword else "")}]
    lines = ["本群 FAQ："]
    for f in faqs:
        lines.append(f"#{f['id']} Q：{f['question']}")
        lines.append(f"   A：{f['answer']}")
    return [{"group_id": group_id, "text": "\n".join(lines)}]


def cmd_faq_add(group_id: str, sender: str, args: str) -> list[dict]:
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可添加 FAQ"}]
    if "=" not in args:
        return [{"group_id": group_id, "text": "用法：/FAQ添加 <问题> = <答案>"}]
    q, a = args.split("=", 1)
    try:
        fid = db.add_faq(group_id, q.strip(), a.strip(), source="老师手工添加", operator=sender)
    except ValueError as e:
        return [{"group_id": group_id, "text": str(e)}]
    return [{"group_id": group_id, "text": f"已添加 FAQ #{fid}"}]


def cmd_faq_delete(group_id: str, sender: str, args: str) -> list[dict]:
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可删除 FAQ"}]
    try:
        fid = int(args.strip().split()[0])
    except (ValueError, IndexError):
        return [{"group_id": group_id, "text": "用法：/FAQ删除 <id>"}]
    ok = db.delete_faq(group_id, fid, sender)
    if not ok:
        return [{"group_id": group_id, "text": f"FAQ #{fid} 不存在或不属于本群"}]
    return [{"group_id": group_id, "text": f"已删除 FAQ #{fid}"}]


def cmd_ask(group_id: str, sender: str, args: str) -> list[dict]:
    """记录未答问题。若 FAQ 已命中（上层先查过），不会走到这里。"""
    question = args.strip()
    if not question:
        return [{"group_id": group_id, "text": "用法：/提问 <问题>"}]
    qid = db.add_unanswered_question(group_id, question, sender)
    return [{"group_id": group_id, "text": f"已记录问题 #{qid}，老师看到后会回答（回答后会加入 FAQ）"}]


def cmd_question_list(group_id: str, sender: str, args: str) -> list[dict]:
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可查看未答问题"}]
    items = db.list_unanswered(group_id, status="open")
    if not items:
        return [{"group_id": group_id, "text": "暂无未答问题"}]
    lines = ["未答问题："]
    for q in items:
        lines.append(f"#{q['id']} {q['question']}（提问者：{q['asker'] or '未知'}）")
    lines.append("回答后请用 /FAQ添加 <问题> = <答案> 沉淀为 FAQ")
    return [{"group_id": group_id, "text": "\n".join(lines)}]

# ---------------------------------------------------------------------
# 阶段四：抓取与提醒状态
# ---------------------------------------------------------------------

def cmd_fetch_status(group_id: str, sender: str, args: str) -> list[dict]:
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可查看抓取状态"}]
    sources = db.list_sources()
    if not sources:
        return [{"group_id": group_id, "text": "暂无公共来源配置"}]
    lines = ["公共来源抓取状态："]
    for s in sources:
        comp = db.get_competition(s["competition_id"])
        name = comp["name"] if comp else f"#{s['competition_id']}"
        st = s["last_status"] or "未抓取"
        lines.append(f"#{s['id']} {name}｜{st}｜{s['last_fetched_at'] or '-'}｜{s['url'][:50]}")
    return [{"group_id": group_id, "text": "\n".join(lines)}]


def cmd_reminder_status(group_id: str, sender: str, args: str) -> list[dict]:
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可查看提醒状态"}]
    pending = [d for d in db.list_pending_deliveries() if d["group_id"] == group_id]
    state = "主动群消息：已启用" if config.GROUP_ACTIVE_MESSAGES_ENABLED else "主动群消息：未启用（定时提醒暂停）"
    if not pending:
        return [{"group_id": group_id, "text": state + "\n本群当前无待发送提醒"}]
    lines = [state, "待发送提醒："]
    for d in pending[:20]:
        lines.append(f"{d['obj_type']}#{d['obj_id']}｜{d['remind_type']}｜目标 {d['target_time']}｜已尝试 {d['attempts']} 次")
    return [{"group_id": group_id, "text": "\n".join(lines)}]


async def cmd_trigger_fetch(group_id: str, sender: str, args: str) -> list[dict]:
    """手动触发一次公共来源抓取（老师）。结果汇总后返回，不写入发送日志冒充发送。

    异步执行：在现有事件循环中调度，避免嵌套 asyncio.run。
    """
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可触发抓取"}]
    try:
        results = await crawler.run_fetch()
    except Exception as e:
        return [{"group_id": group_id, "text": f"抓取失败：{e}"}]
    if not results:
        return [{"group_id": group_id, "text": "暂无公共来源配置"}]
    lines = [f"本轮抓取 {len(results)} 个来源："]
    for r in results:
        if r["kind"] == "skip":
            lines.append(f"#{r['source_id']} 跳过：{r['reason']}")
        elif r["kind"] == "nochange":
            lines.append(f"#{r['source_id']} 无变化（{r['name']}）")
        elif r["kind"] == "disabled":
            lines.append("自动抓取未开启（AUTO_FETCH_ENABLED=False）")
        elif r["kind"] == "auto_created":
            lines.append(f"#{r['source_id']} 免审新建比赛：{r['name']}（id={r['competition_id']}，已 confirmed，未通知全员）")
        elif r["kind"] == "pending_auto":
            lines.append(f"#{r['source_id']} 有更新已存待审：{r['name']}（比赛 id={r['competition_id']}，更新 id={r['update_id']}，未通知全员）")
        elif r["kind"] == "updated_auto":
            lines.append(f"#{r['source_id']} 更新草稿比赛：{r['name']}（id={r['competition_id']}，未通知全员）")
        else:
            lines.append(f"#{r['source_id']} 异常：{r['error']}")
    return [{"group_id": group_id, "text": "\n".join(lines)}]


# ---------------------------------------------------------------------
# 分发器
# ---------------------------------------------------------------------

async def handle_command(text: str, group_id: str, sender: str) -> list[dict] | None:
    """处理一条命令消息。

    - 非命令消息返回 None（由上层走 FAQ 匹配 / 模型问答 / 未答问题收集）。
    - 命令消息返回消息列表，每条含 group_id 与 text（text 为空表示无需发送）。
    - 命令名大小写不敏感：精确匹配失败时按 lower 兜底（兼容 /FAQ、/faq 等）。
    - 命令处理函数若为协程（如异步抓取）则在本循环内 await，避免嵌套 asyncio.run。
    """
    if not text.startswith("/"):
        return None
    cmd, args = parse_command(text)
    if cmd is None:
        return [{"group_id": group_id, "text": "无法识别的命令，用 /帮助 查看所有命令"}]
    if cmd in ("help", "帮助"):
        return [{"group_id": group_id, "text": HELP_TEXT}]
    handlers = {
        "修正": cmd_amend,
        "征集": cmd_collection_create,
        "报名": cmd_collection_register,
        "征集汇总": cmd_collection_summary,
        "征集关闭": cmd_collection_close,
        "校安排": cmd_org_events,
        "课程列表": cmd_training_list,
        "课程添加": cmd_training_add,
        "课程取消": cmd_training_cancel,
        "FAQ列表": cmd_faq_list,
        "FAQ添加": cmd_faq_add,
        "FAQ删除": cmd_faq_delete,
        "提问": cmd_ask,
        "问题列表": cmd_question_list,
        "抓取状态": cmd_fetch_status,
        "提醒状态": cmd_reminder_status,
        "抓取一次": cmd_trigger_fetch,
    }
    handler = handlers.get(cmd)
    if handler is None:
        lower = cmd.lower()
        for key, h in handlers.items():
            if key.lower() == lower:
                handler = h
                break
    if handler is None:
        return [{"group_id": group_id, "text": f"命令 /{cmd} 暂不支持，用 /帮助 查看所有命令"}]
    result = handler(group_id, sender, args)
    if asyncio.iscoroutine(result):
        result = await result
    return result


def match_faq(group_id: str, text: str) -> dict | None:
    """在群 FAQ 中匹配问题：命中返回 FAQ 记录（含 id/question/answer），未命中返回 None。"""
    for faq in db.list_faq(group_id):
        if faq["question"] in text:
            return faq
        q_words = [w for w in faq["question"].replace("？", " ").replace("?", " ").split() if len(w) >= 2]
        if any(w in text for w in q_words):
            return faq
    return None


async def handle_non_command_message(text: str, group_id: str, sender: str) -> list[dict]:
    """非命令消息：FAQ 匹配 → 模型问答 → 模型不可用/无解时才收集为未答问题。"""
    faq = match_faq(group_id, text)
    if faq is not None:
        return [{"group_id": group_id, "text": f"Q：{faq['question']}\nA：{faq['answer']}"}]
    try:
        answer = await llm.answer_question(text, db.list_question_contests(group_id))
    except Exception:  # noqa: BLE001
        answer = "模型暂时不可用，请稍后再试~"
    if answer.startswith("模型暂时不可用"):
        qid = db.add_unanswered_question(group_id, text, sender)
        return [{"group_id": group_id, "text": f"收到，已记录为问题 #{qid}，老师看到后会回答"}]
    return [{"group_id": group_id, "text": answer}]
