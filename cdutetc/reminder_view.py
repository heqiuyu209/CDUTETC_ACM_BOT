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
        result.append({"name": name, "scheduled_at": scheduled, "target_time": target,
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
    return sorted(result, key=lambda item: (item["scheduled_at"], item["name"], item["key"]))


def render(group_id: str, *, page: int = 1, now: datetime | None = None) -> str:
    if page < 1:
        return "页码应从 1 开始。"
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
    if not plans:
        lines.append("本群未来7天暂无提醒计划（不代表其他群也没有）。")
    else:
        pages = (len(plans) + 9) // 10
        if page > pages:
            return f"共 {pages} 页，请发送 /提醒状态 1～{pages}。"
        lines.append(f"共{len(plans)}项 · 第{page}/{pages}页")
        for plan in plans[(page - 1) * 10:page * 10]:
            lines.append(f"{plan['scheduled_at']:%m-%d %H:%M}｜{plan['name']}｜{plan['label']}｜{plan['state']}"
                         + (f" [{plan['error']}]" if plan["error"] else ""))
        if pages > page:
            lines.append(f"下一页：/提醒状态 {page + 1}")
        if any(plan["state"] in ("待扫描", "未排队") for plan in plans):
            lines.append("待扫描：到窗口后登记；未排队：尚无持久计划，不能当作已安排。")
    counts = db.delivery_status_counts(group_id)
    lines.append("投递统计：" + "｜".join(f"{text} {counts.get(key, 0)}" for key, text in STATUS.items()))
    pending = db.list_deliveries(group_id, statuses=("pending", "sending"), limit=20)
    if not pending:
        lines.append("本群当前无待发送提醒")
    # 保留过期/失败诊断，避免只有未来预览而看不见遗留的队列状态。
    visible = {item["key"] for item in plans}
    for row in [*pending, *db.list_deliveries(group_id, statuses=("failed", "expired"), limit=5)]:
        if row["dedup_key"] in visible:
            continue
        if row["obj_type"] == "competition" and group_id not in db.get_competition_target_groups(row["obj_id"]):
            continue
        lines.append(f"队列记录 {row['obj_type']}#{row['obj_id']}｜{STATUS[row['status']]}｜目标 {row['target_time']}"
                     + (f"｜错误 [{row['error_code']}]" if row.get("error_code") else ""))
    return "\n".join(lines)
