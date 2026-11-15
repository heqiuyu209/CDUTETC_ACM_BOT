"""主动发信预检：把「查得到」与「到点能推」之间的多道闸门显式化。

设计要点：
- **只读**：不写库、不发信、不登记投递，可被管理视图与诊断指令任意调用；
- 「读」只看数据库记录，「推」还要过群白名单、接入能力位、主动消息总开关、
  群级熔断（平台 11253/11263 配额拒绝）、比赛与群的绑定关系、调度节点流水，
  本模块把这六道闸门按顺序串起来，任一不通过即给出可操作的阻断原因；
- 与 :mod:`cdutetc.reminder_view` 共用 ``_parse_time``，避免时间口径漂移。
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from cdutetc import config
from cdutetc import database as db
from cdutetc.clients.base import active_capabilities, active_protocol
from cdutetc.reminder import _parse_time

TZ = ZoneInfo(config.TZ_NAME)
PENDING_STATUSES = ("pending", "sending")
RULE = "-" * 33
FOOTER = "💡 只有标记为【主动提醒就绪】的比赛到点才会由机器人主动推送。"


def group_label(group_id: str) -> str:
    """群的可读标识：命名 > 动态白名单备注 > 群号/openid。"""
    label = db.get_account_label("group", group_id)
    if label:
        return label
    note = next((item["note"] for item in db.list_runtime_groups()
                 if item["group_id"] == group_id and item["note"]), "")
    return note or group_id


def _offset_label(scheduled: datetime | None, target: datetime | None) -> str:
    if scheduled is None or target is None:
        return ""
    minutes = round((target - scheduled).total_seconds() / 60)
    if minutes <= 0:
        return "开赛时刻"
    return f"赛前 {minutes} 分钟"


def _next_node(contest_id, group_id: str, now: datetime):
    """返回本群本比赛最近一个未来待发节点：(scheduled_at, target_time) 或 None。"""
    best = None
    for row in db.list_deliveries(group_id, statuses=PENDING_STATUSES, limit=500):
        if row.get("obj_type") != "competition" or str(row.get("obj_id")) != str(contest_id):
            continue
        if row.get("remind_type") != "contest_start":
            continue
        scheduled = _parse_time(row.get("scheduled_at") or "") or _parse_time(row.get("target_time") or "")
        if scheduled is None or scheduled <= now:
            continue
        if best is None or scheduled < best[0]:
            best = (scheduled, _parse_time(row.get("target_time") or ""))
    return best


def _evaluate(contest_id, group_id: str, now: datetime):
    """完整预检，附带结构化时间：返回 (是否就绪, 说明, 提醒时间, 开赛时间)。"""
    gid = (group_id or "").strip()
    try:
        cid = int(str(contest_id).strip().lstrip("#"))
    except (TypeError, ValueError):
        return False, "阻断原因: 比赛编号无效", None, None

    contest = db.get_competition(cid)
    if not contest:
        return False, f"阻断原因: 比赛 #{cid} 不存在", None, None
    status = (contest.get("status") or "").strip()
    if status != "confirmed":
        return False, f"阻断原因: 比赛 #{cid} 尚未确认（当前 {status or '未知'}）", None, None

    # 1. 群白名单
    if gid not in config.ALLOWED_GROUPS:
        return False, f"阻断原因: 群 {gid} 未授权（不在允许群白名单）", None, None

    # 2. 接入能力位与主动发信开关、群级熔断
    if not active_capabilities().accepts_group_id(gid):
        return False, f"阻断原因: 当前接入（{active_protocol()}）不支持向该群标识主动发信", None, None
    if not config.GROUP_ACTIVE_MESSAGES_ENABLED:
        return False, "阻断原因: 主动群消息总开关已关闭（ACM_GROUP_ACTIVE_MESSAGES_ENABLED）", None, None
    block = db.get_group_notification_block(active_protocol(), gid)
    if block:
        return (False,
                f"阻断原因: 平台拒绝主动消息，本群已熔断暂停 "
                f"[{block.get('error_code') or '未知'}/{block.get('platform_code')}]",
                None, None)

    # 3. 比赛与群的绑定关系
    if db.is_weekly_competition(cid):
        if gid not in db.get_weekly_groups():
            return False, "阻断原因: 本群未加入 ACM_WEEKLY_CONTEST_GROUPS", None, None
        if config.WEEKLY_CONTEST_GROUPS and gid not in config.WEEKLY_CONTEST_GROUPS:
            return False, "阻断原因: 本群未配置在 ACM_WEEKLY_CONTEST_GROUPS", None, None
        if gid not in db.get_competition_target_groups(cid):
            return False, "阻断原因: 周赛生成时未向本群注入发信流水", None, None
    elif gid not in db.get_competition_target_groups(cid):
        return False, "阻断原因: 未使用 /通知群 关联本群", None, None

    # 4. 调度节点存在性
    node = _next_node(cid, gid, now)
    if node is None:
        return False, "阻断原因: 无待触发的未来发信节点（时间未定或节点已过期）", None, None
    scheduled, target = node
    return True, f"就绪 (预计提醒时间: {scheduled:%Y-%m-%d %H:%M})", scheduled, target


def check_contest_delivery_readiness(contest_id: int | str, group_id: str,
                                    now: datetime | None = None) -> tuple[bool, str]:
    """预检「这场比赛在这个群到点会不会真的推」。

    Returns:
        (True, "就绪 (预计提醒时间: YYYY-MM-DD HH:MM)") 或
        (False, "阻断原因: <具体原因文本>")
    """
    now = now or datetime.now(TZ)
    now = now.replace(tzinfo=TZ) if now.tzinfo is None else now.astimezone(TZ)
    ok, message, _, _ = _evaluate(contest_id, group_id, now)
    return ok, message


def build_group_diagnosis(group_id: str, *, now: datetime | None = None) -> dict:
    """聚合本群所有已确认比赛的就绪/阻断诊断，供管理视图渲染。"""
    now = now or datetime.now(TZ)
    now = now.replace(tzinfo=TZ) if now.tzinfo is None else now.astimezone(TZ)
    ready: list[dict] = []
    blocked: list[dict] = []
    for contest in db.list_group_competitions(group_id, status="confirmed"):
        ok, message, scheduled, target = _evaluate(contest["id"], group_id, now)
        item = {"id": contest["id"], "name": contest.get("name") or f"#{contest['id']}",
                "when": scheduled, "offset": _offset_label(scheduled, target),
                "reason": "" if ok else message}
        (ready if ok else blocked).append(item)
    ready.sort(key=lambda item: item["when"])
    return {"group_id": group_id, "label": group_label(group_id),
            "ready": ready, "blocked": blocked,
            "active": config.GROUP_ACTIVE_MESSAGES_ENABLED,
            "block": db.get_group_notification_block(active_protocol(), group_id)}


def render_group_block(diagnosis: dict) -> str:
    """渲染单个群的诊断块（就绪 🟢 与阻断 🟡 严格分区）。"""
    head = "主动发信: 开启" if diagnosis["active"] else "主动发信: 关闭"
    block = diagnosis["block"]
    fuse = (f"熔断状态: 暂停 [{block.get('error_code') or '未知'}/{block.get('platform_code')}]"
            if block else "熔断状态: 正常")
    lines = [f"群：{diagnosis['label']} ({head} | {fuse})", RULE]
    if not diagnosis["ready"] and not diagnosis["blocked"]:
        lines.append("近期没有已确认的比赛可诊断（先用 /通知列表 确认比赛是否入库并关联本群）。")
        return "\n".join(lines)

    lines.append("🟢【主动提醒就绪】(到点必推)")
    if not diagnosis["ready"]:
        lines.append("- 暂无")
    for item in diagnosis["ready"]:
        lines.append(f"- [ID: {item['id']}] {item['name']}")
        suffix = f" ({item['offset']})" if item["offset"] else ""
        lines.append(f"  预计提醒: {item['when']:%Y-%m-%d %H:%M}{suffix}")

    lines.append("🟡【已收录但不可主动提醒】(到点不推)")
    if not diagnosis["blocked"]:
        lines.append("- 暂无")
    for item in diagnosis["blocked"]:
        lines.append(f"- [ID: {item['id']}] {item['name']}")
        lines.append(f"  ⚠️ {item['reason']}")
    return "\n".join(lines)


def render_diagnosis(group_ids, *, now: datetime | None = None) -> str:
    """按作用域渲染诊断总览：global 传全部已授权群，group 传当前群。"""
    groups = [gid for gid in group_ids if gid]
    if not groups:
        return "还没有授权群，无法诊断。先用 /加群 添加目标群。"
    blocks = [render_group_block(build_group_diagnosis(gid, now=now)) for gid in groups]
    header = "【CDUTETC ACM 比赛与提醒状态诊断】"
    if len(groups) > 1:
        header += f"\n共 {len(groups)} 个已授权群（私聊全局视图）"
    return "\n\n".join([header, *blocks, RULE, FOOTER])
