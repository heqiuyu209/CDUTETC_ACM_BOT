"""只读提醒预览：复用业务时间、节点和去重键，不等待任务到点也不产生投递。"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from cdutetc import config, database as db
from cdutetc.clients.base import active_capabilities, active_protocol
from cdutetc.reminder import _due_offset_hours, _parse_time

TZ = ZoneInfo(config.TZ_NAME)
STATUS = {"pending": "待发送", "sending": "发送中", "sent": "已发送",
          "failed": "重试耗尽", "cancelled": "已取消", "expired": "已过期"}


def build_preview(group_id: str, now: datetime | None = None, days: int = 7) -> list[dict]:
    now = now or datetime.now(TZ)
    now = now.replace(tzinfo=TZ) if now.tzinfo is None else now.astimezone(TZ)
    until = now + timedelta(days=days)
    deliveries = {d["dedup_key"]: d for d in db.list_deliveries(group_id, limit=10000)}
    result = []

    def add(kind, oid, name, field, target_text, offset, label, *, persisted=False):
        target = _parse_time(target_text)
        if target is None or target <= now:
            return
        scheduled = target - timedelta(hours=offset)
        key = db.delivery_dedup_key(group_id, kind, oid, field, target_text, offset)
        row = deliveries.get(key) or deliveries.get(db.delivery_dedup_key(group_id, kind, oid, field, target_text))
        if persisted and row:
            stored = _parse_time(row.get("scheduled_at") or "")
            expires = _parse_time(row.get("expires_at") or "")
            if stored is not None and expires is not None:
                scheduled = stored
            else:
                row = None
        if scheduled > until:
            return
        if scheduled < now:
            if persisted:
                expires = _parse_time(row.get("expires_at") or "") if row else None
                if not row or row["status"] not in ("pending", "sending") or expires is None or expires < now:
                    return
            elif _due_offset_hours(target - now) != offset or (row and row["status"] == "sent"):
                return
        state = STATUS.get(row["status"], row["status"]) if row else ("未排队" if persisted else "待扫描")
        result.append({"name": name, "obj_type": kind, "obj_id": oid, "field": field,
                       "scheduled_at": scheduled, "target_time": target,
                       "label": label, "state": state, "key": key,
                       "error": (row.get("error_code") or "") if row else ""})

    for contest in db.list_competitions(status="confirmed"):
        if group_id not in db.get_competition_target_groups(contest["id"]):
            continue
        for minutes in config.CONTEST_REMIND_OFFSETS_MINUTES:
            add("competition", contest["id"], contest["name"], "contest_start", contest["official_time"],
                minutes / 60, f"开赛前{minutes}分钟", persisted=True)
        for hours in config.REMIND_OFFSET_HOURS:
            add("competition", contest["id"], contest["name"], "reg_deadline", contest["official_reg_deadline"],
                hours, f"报名截止前{hours:g}小时")
    for collection in db.list_collections(group_id, status="open"):
        for hours in config.REMIND_OFFSET_HOURS:
            add("collection", collection["id"], collection["title"], "deadline", collection["deadline"],
                hours, f"征集截止前{hours:g}小时")
    for session in db.list_training_sessions(group_id, active_only=False):
        for target in db.training_occurrence_times(session, now, days * 24 + max(config.REMIND_OFFSET_HOURS)):
            for hours in config.REMIND_OFFSET_HOURS:
                add("training_session", session["id"], session["title"], "start_time", target.strftime("%Y-%m-%d %H:%M"),
                    hours, f"开课前{hours:g}小时")
    return sorted(result, key=lambda item: (item["scheduled_at"], item["obj_id"], item["key"]))


FIELD_TARGET_LABELS = {"contest_start": "开赛", "reg_deadline": "报名截止",
                       "deadline": "截止", "start_time": "开课"}
DELETED_TARGET_LABELS = {"competition": "已删除赛事", "collection": "已删除征集",
                         "training_session": "已删除课程"}


def _short_time(moment) -> str:
    return f"{moment:%m-%d %H:%M}" if moment else ""


def _target_text(obj_type: str, obj_id: int, names: dict) -> str:
    """把裸类型#ID 还原为「名称 (#ID)」，目标已删除时给出兜底文案。"""
    name = names.get((obj_type, int(obj_id))) if obj_id else None
    return f"{name or DELETED_TARGET_LABELS.get(obj_type, '已删除目标')} (#{obj_id})"


def _queue_line(row: dict, names: dict) -> str:
    """队列记录统一格式：触发时间（目标时间）｜名称 (#ID)｜状态｜错误。"""
    fire = _parse_time(row.get("scheduled_at") or "")
    target = _parse_time(row.get("target_time") or "")
    when = _short_time(fire) or (row.get("scheduled_at") or "--")
    if target:
        kind = FIELD_TARGET_LABELS.get(row.get("remind_type") or "", "目标")
        when += f"（{kind} {_short_time(target)}）"
    line = f"{when}｜{_target_text(row['obj_type'], row['obj_id'], names)}｜{STATUS.get(row['status'], row['status'])}"
    if row.get("error_code"):
        line += f"｜错误 [{row['error_code']}]"
    return line


def render(group_id: str, *, page: int = 1, now: datetime | None = None) -> str:
    if page < 1:
        return "页码应从 1 开始。"
    now = now or datetime.now(TZ)
    now = now.replace(tzinfo=TZ) if now.tzinfo is None else now.astimezone(TZ)
    horizon = now + timedelta(days=7)
    plans = build_preview(group_id, now)
    label = db.get_account_label("group", group_id) or "本群"
    state = "已启用" if config.GROUP_ACTIVE_MESSAGES_ENABLED else "未启用（定时提醒暂停）"
    lines = [f"【{label}主动提醒】未来7天 · 北京时间", f"主动群消息：{state}"]
    capabilities = active_capabilities()
    protocol = active_protocol()
    # 只有存在主动推送权限约束的接入才可能有暂停记录与格式记忆。
    if capabilities.supports_passive_reply:
        block = db.get_group_notification_block(protocol, group_id)
        if block:
            lines.append(f"平台权限拒绝，已暂停 [{block['error_code']}/{block['platform_code']}]")
            lines.append("恢复群主授权后，由超管执行 /通知测试 群 验证。")
        fmt = db.get_group_notification_format(protocol, group_id)
        lines.append("本群主动通知格式：" + ("Markdown" if fmt == "markdown" else "文本"))
    # 上半部分已经展示过的提醒不再出现在队列段，同一场比赛只出现一次。
    visible = {item["key"] for item in plans}
    scope: dict[int, set[str]] = {}

    def in_scope(row: dict) -> bool:
        if row["obj_type"] != "competition":
            return True
        cid = row["obj_id"]
        if cid not in scope:
            scope[cid] = set(db.get_competition_target_groups(cid))
        return group_id in scope[cid]

    rows = [row for row in db.list_deliveries(
        group_id, statuses=("pending", "sending", "failed", "expired"), order="schedule", limit=200,
    ) if row["dedup_key"] not in visible and in_scope(row)]
    names = db.get_target_names((row["obj_type"], row["obj_id"]) for row in rows)
    issues = [row for row in rows if row["status"] in ("failed", "expired")]
    waiting = [row for row in rows if row["status"] in ("pending", "sending")]
    later = [row for row in waiting if (_parse_time(row.get("scheduled_at") or "") or now) > horizon]
    same_window = [row for row in waiting if (_parse_time(row.get("scheduled_at") or "") or now) <= horizon]
    # 异常（重试耗尽/过期）置顶，先给诊断再排日程。
    if issues and page == 1:
        tally: dict[str, int] = {}
        for row in issues:
            tally[row["status"]] = tally.get(row["status"], 0) + 1
        lines.append(f"异常队列 {len(issues)} 项（"
                     + "｜".join(f"{STATUS.get(key, key)} {value}" for key, value in tally.items()) + "）")
        for row in issues[:5]:
            lines.append("异常｜" + _queue_line(row, names))
        if len(issues) > 5:
            lines.append(f"异常｜其余 {len(issues) - 5} 项未列出")
    pages = 1
    if not plans:
        lines.append("本群未来7天暂无提醒计划（不代表其他群也没有）。")
    else:
        pages = (len(plans) + 9) // 10
        if page > pages:
            return f"共 {pages} 页，请发送 /提醒状态 1～{pages}。"
        lines.append(f"共{len(plans)}项 · 第{page}/{pages}页")
        for plan in plans[(page - 1) * 10:page * 10]:
            when = _short_time(plan["scheduled_at"])
            if plan.get("target_time"):
                kind = FIELD_TARGET_LABELS.get(plan.get("field") or "", "目标")
                when += f"（{kind} {_short_time(plan['target_time'])}）"
            lines.append(f"{when}｜{plan['name']} (#{plan['obj_id']})｜{plan['label']}｜{plan['state']}"
                         + (f" [{plan['error']}]" if plan["error"] else ""))
        if pages > page:
            lines.append(f"下一页：/提醒状态 {page + 1}")
        if any(plan["state"] in ("待扫描", "未排队") for plan in plans):
            lines.append("待扫描：到窗口后登记；未排队：尚无持久计划，不能当作已安排。")
    counts = db.delivery_status_counts(group_id)
    lines.append("投递统计：" + "｜".join(f"{text} {counts.get(key, 0)}" for key, text in STATUS.items()))
    if page >= pages:
        if later:
            lines.append(f"7天后已排期队列 {len(later)} 项（按触发时间由近到远）：")
            for row in later[:20]:
                lines.append(_queue_line(row, names))
            if len(later) > 20:
                lines.append(f"其余 {len(later) - 20} 项未列出")
        else:
            lines.append("7天后暂无已排期提醒")
        # 7 天内但未进入日程预览的待发送记录，单独列出以免被静默隐藏。
        if same_window:
            lines.append(f"7天内待发送（未排入日程）{len(same_window)} 项：")
            for row in same_window[:10]:
                lines.append(_queue_line(row, names))
    return "\n".join(lines)
