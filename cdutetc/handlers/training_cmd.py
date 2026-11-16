"""训练与提醒命令：/课程增删改、校历事件、提醒增删改查、摘要预览。"""

import logging
from datetime import datetime, timedelta
from cdutetc import database as db
from cdutetc.utils import can_manage_group, is_super_admin

from cdutetc.handlers import _shared as _shared
from cdutetc.handlers import admin_cmd as _admin_cmd

logger = logging.getLogger(__name__)


def cmd_org_events(group_id: str, sender: str, args: str) -> list[dict]:
    """查看学校统一安排（仅共享给群的 confirmed 安排）。"""
    events = db.list_org_events(status="confirmed", shareable_only=True)
    if not events:
        return [{"group_id": group_id, "text": "当前没有已共享的学校安排"}]
    lines = ["学校统一安排："]
    for e in events:
        t = e["contest_time"] or e["reg_deadline"] or ""
        lines.append(f"#{e['id']} {e['title']}｜{t}｜{(e.get('location') or '').strip() or '待定'}")
    return [{"group_id": group_id, "text": "\n".join(lines)}]


def cmd_training_list(group_id: str, sender: str, args: str) -> list[dict]:
    """课程列表：每门课一行标题 + 缩进的「时间/地点/要求」多行卡片式排版。

    旧版把名称、时间、地点、要求用「｜」挤成一行，要求一长就整行糊住；
    现在改为缩进多行，要求为空时整行省略，操作提示统一收到列表最底部。
    """
    sessions = db.list_training_sessions(group_id)
    if not sessions:
        return [{"group_id": group_id, "text": "本群暂无课程安排"}]
    lines = ["本群课程安排："]
    for index, s in enumerate(sessions, 1):
        need = "（需带电脑）" if s["needs_laptop"] else ""
        # 只展示 1-based 序号：课程 id 与序号常在删课后错位，同时出现会让人改错课；
        # 内部仍兼容 #id，只是列表里不再渲染。
        lines.append(f"{index}. {s['title']}{need}")
        span = _shared._format_course_time(
            s.get("weekday") or "", s.get("start_time") or "", s.get("end_time") or ""
        )
        lines.append(f"{_shared._COURSE_LIST_INDENT}时间：{span or '待定'}")
        lines.append(f"{_shared._COURSE_LIST_INDENT}地点：{_shared._course_location_display(s.get('location'))}")
        requirement = (s.get("requirement") or "").strip()
        if requirement:
            lines.append(f"{_shared._COURSE_LIST_INDENT}要求：{requirement}")
    lines.append("")
    lines.append(_shared._COURSE_LIST_MODIFY_TIP)
    return [{"group_id": group_id, "text": "\n".join(lines)}]


def cmd_training_add(group_id: str, sender: str, args: str) -> list[dict]:
    """新增课程：兼容位置参数，也支持 `时间=/地点=/电脑=` 命名参数。"""
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可添加课程"}]
    fields, rest = _shared._split_course_fields(args)
    parts = rest.split(None, 3)
    title = fields.get("标题") or ""
    if not title and parts:
        title = parts[0]
    if not title:
        return [{"group_id": group_id, "text": _shared._TRAINING_ADD_USAGE}]
    # 口径：标题只放课程名；内容=/要求= 属于「要求」，与标题互不覆盖。
    raw_requirement = (fields.get("要求") or fields.get("内容") or "").strip()
    weekday = "" if fields.get("时间") else (parts[1] if len(parts) > 1 else "")
    time_range = "" if fields.get("时间") else (parts[2] if len(parts) > 2 else "")
    location = _shared._normalize_location_text(
        fields["地点"] if "地点" in fields else (parts[3] if len(parts) > 3 else "")
    )
    if _shared._is_location_cleared(location):
        location = ""
    elif _shared._looks_like_requirement(location):
        # 位置参数把「要求」挤到地点位（如 `/课程添加 训练 周四 19-21点 自带电脑`）时，
        # 这段文本还给「要求」，地点留空——绝不把规则长文写进 location。
        raw_requirement = " ".join(x for x in (raw_requirement, location) if x)
        location = ""
    raw_time = fields.get("时间") or " ".join(x for x in (weekday, time_range) if x)
    time_fields = _shared._apply_time_fields(raw_time) or _shared._fallback_time_fields(weekday, time_range)
    info: dict[str, object] = {
        "title": title,
        "weekday": time_fields.get("weekday", weekday),
        "start_time": time_fields.get("start_time", ""),
        "end_time": time_fields.get("end_time", ""),
        "location": location,
    }
    for key, field in (("软件", "software"), ("设备", "device_req")):
        if fields.get(key):
            info[field] = fields[key]
    if "电脑" in fields:
        laptop = fields["电脑"].strip()
        if laptop in ("需要", "是", "要", "1"):
            info["needs_laptop"] = 1
        elif laptop in ("不需要", "否", "不用", "0"):
            info["needs_laptop"] = 0
    requirement, summarized = _shared._prepare_requirement(raw_requirement, title)
    if requirement:
        info["requirement"] = requirement
    tid = db.create_training_session(group_id, info, sender)
    span = _shared._format_course_time(info["weekday"], info["start_time"], info["end_time"])
    tail = f"（{span}）" if (info["start_time"] or info["end_time"]) and span else ""
    text = f"已添加课程 #{tid}：{title}{tail}"
    if requirement:
        text += f"\n要求：{requirement}"
        if summarized:
            text += "（已智能提炼）"
    return [{"group_id": group_id, "text": text}]


def cmd_training_modify(group_id: str, sender: str, args: str) -> list[dict]:
    """修改课程：目标支持列表序号 / #id / 课程名；时间与标题可写在位置上。

    两种写法等价：`/课程修改 1 时间=周四 19:00-21:00`（键值对）与
    `/课程修改 1 周四 19:00-21:00 加练`（位置参数）；同时出现时键值对优先。
    """
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可修改课程"}]
    fields, rest = _shared._split_course_fields(args)
    if not rest.strip():
        return [{"group_id": group_id, "text": _shared._TRAINING_MODIFY_USAGE}]
    course, ambiguous, consumed = _shared._resolve_course_target_with_span(group_id, rest)
    if course is None:
        target = " ".join(rest.split()[:max(consumed, 1)])
        return [{"group_id": group_id, "text": ambiguous or f"找不到课程：{target}"}]
    tail = " ".join(rest.split()[consumed:])
    positional_time, positional_text = _shared._extract_time_span(tail)
    updates: dict[str, object] = {}
    notices: list[str] = []
    # 口径：标题= 只改课程名；内容=/要求=/位置上的尾随文本都写「要求」，互不覆盖。
    title_field = (fields.get("标题") or "").strip()
    requirement_field = (fields.get("要求") or fields.get("内容") or "").strip()
    if title_field:
        updates["title"] = title_field
    if requirement_field:
        updates["requirement"] = requirement_field
    elif positional_text:
        # 位置参数里时间之外的部分（如 `1 周四 19-21点 加练双指针`）是要求补充。
        updates["requirement"] = positional_text
    if fields.get("时间"):
        updates.update(_shared._apply_time_fields(fields["时间"]))
        if positional_time:
            notices.append("已忽略位置参数里的时间（键值对 时间= 优先）")
    elif positional_time:
        updates.update(positional_time)
    for key, field in (("软件", "software"), ("设备", "device_req")):
        if fields.get(key):
            updates[field] = fields[key]
    if "地点" in fields:
        # 地点三态：清空词 → 置空；像要求长文 → 拒写并提示；否则按地址入库。
        location_text = _shared._normalize_location_text(fields["地点"])
        if _shared._is_location_cleared(location_text):
            updates["location"] = ""
        elif _shared._looks_like_requirement(location_text):
            notices.append("地点= 的值更像课程要求，已忽略；写要求请用 要求=")
        else:
            updates["location"] = location_text
    if "电脑" in fields:
        laptop = fields["电脑"].strip()
        if laptop in ("需要", "是", "要", "1"):
            updates["needs_laptop"] = True
        elif laptop in ("不需要", "否", "不用", "0"):
            updates["needs_laptop"] = False
        else:
            notices.append("电脑= 只支持 需要/不需要")
    if not updates:
        # 形如 `地点=<一段要求>` 被拒写时仍要把原因告诉用户，别只回一句用法。
        if notices:
            return [{"group_id": group_id, "text": "\n".join(notices)}]
        return [{"group_id": group_id, "text": _shared._TRAINING_MODIFY_USAGE}]
    summarized = False
    if "requirement" in updates:
        updated_title = str(updates.get("title") or course.get("title") or "")
        prepared, summarized = _shared._prepare_requirement(str(updates["requirement"]), updated_title)
        if not prepared:
            updates.pop("requirement", None)
        else:
            updates["requirement"] = prepared
            if not updates:
                return [{"group_id": group_id, "text": _shared._TRAINING_MODIFY_USAGE}]
    ok = db.update_group_course(group_id, course["id"], sender, **updates)
    if not ok:
        # 这里目标已解析成功、只是落库失败；用课程自身标识回报。
        # 不能复用只在「目标解析失败」分支里定义的 target，否则会 UnboundLocalError。
        failed_label = (course.get("title") or "").strip() or str(course.get("id") or "")
        return [{"group_id": group_id, "text": f"找不到课程：{failed_label}"}]
    updated = db.get_group_course(group_id, course["id"]) or course
    parts: list[str] = []
    if "title" in updates:
        parts.append(f"标题={updated['title']}")
    if {"weekday", "start_time", "end_time"} & set(updates):
        span = _shared._format_course_time(updated.get("weekday", ""), updated.get("start_time", ""),
                                   updated.get("end_time", ""))
        if span:
            parts.append(f"时间={span}")
    if "location" in updates:
        cleared_location = (updated.get("location") or "").strip()
        parts.append(f"地点={cleared_location or '已清空（列表显示待定）'}")
    if "needs_laptop" in updates:
        parts.append(f"需带电脑={'需要' if updated.get('needs_laptop') else '不需要'}")
    if "software" in updates:
        parts.append(f"软件={updated.get('software') or '无'}")
    if "device_req" in updates:
        parts.append(f"设备={updated.get('device_req') or '无'}")
    if "requirement" in updates:
        parts.append(f"要求={updated.get('requirement') or '无'}")
    # 回执用标题定位课程，避免用户输入列表序号 1 却看到数据库 id 造成混淆；
    # 本次未改标题时 updated 即原记录，展示原标题。
    display_title = (updated.get("title") or course.get("title") or "").strip()
    label = f"「{display_title}」" if display_title else ""
    text = f"已更新课程{label}：" + "｜".join(parts or ["无变更"])
    if summarized:
        text += "（要求已智能提炼）"
    if notices:
        text += "\n" + "\n".join(notices)
    return [{"group_id": group_id, "text": text}]


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


def cmd_reminder_status(group_id: str, sender: str, args: str) -> list[dict]:
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可查看提醒状态"}]
    from cdutetc import reminder_view
    try:
        page = int(args.strip() or "1")
    except ValueError:
        return [{"group_id": group_id, "text": "用法：/提醒状态 [页码]"}]
    return [{"group_id": group_id, "text": reminder_view.render(group_id, page=page)}]


DIGEST_PREVIEW_EMPTY = (
    "[预览] 该群今日无课程、无临近截止征集且无就绪比赛，晨报将跳过发送（不消耗配额）。"
)


DIGEST_PREVIEW_PREFIX = "【晨报私聊模拟预览】"


def cmd_digest_preview(group_id: str, sender: str, args: str, *, private: bool = False) -> list[dict]:
    """/晨报预览：只在当前通道回显晨报渲染结果，不触发任何群发送。

    私聊绑定目标群时，检验该群今日晨报会是什么样；群内则由老师/超管自查本群。
    """
    from cdutetc import daily_digest
    if private:
        if not is_super_admin(sender):
            return [{"group_id": group_id, "text": _shared.SCOPE_DENIED_PRIVATE}]
    elif _admin_cmd.admin_scope(sender, group_id, private=False) is None:
        return [{"group_id": group_id, "text": _shared.SCOPE_DENIED_GROUP}]
    # 只读渲染，绝不调用 send_group：预览结果由调用方原路（私聊）回显
    text = daily_digest.build_daily_digest(group_id)
    header = f"{DIGEST_PREVIEW_PREFIX}目标群：{_shared._group_label(group_id)}（{group_id}）\n"
    if not text:
        return [{"group_id": group_id, "text": header + DIGEST_PREVIEW_EMPTY}]
    return [{"group_id": group_id,
             "text": header + "以下内容仅本次预览，未发送到群：\n" + text}]


def _now_local() -> datetime:
    return datetime.now(_shared.TZ)


def _next_weekly_occurrence(weekday: str, hour: int, minute: int, now: datetime) -> datetime:
    index = _shared._WEEKDAY_INDEX[weekday]
    base = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    delta = (index - base.weekday()) % 7
    target = base + timedelta(days=delta)
    if target <= now:
        target += timedelta(days=7)
    return target


def parse_reminder_time(prefix: str, now: datetime) -> tuple[datetime, str, str, str, str]:
    """解析「每周四 19:00」或「2026-11-20 18:00」式时间前缀。

    返回 (下次触发时间, 循环规则, 周几, 时刻文本, 剩余未消费文本)。
    """
    match = _shared._WEEKLY_PREFIX.match(prefix)
    if match:
        weekday = match.group(1)
        hour = int(match.group(2))
        minute = int(match.group(3) or 0)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("时间超出范围，小时 0-23、分钟 0-59")
        target = _next_weekly_occurrence(weekday, hour, minute, now)
        time_of_day = f"{hour:02d}:{minute:02d}"
        return (target, "weekly", weekday, time_of_day, prefix[match.end():])
    match = _shared._ABSOLUTE_PREFIX.match(prefix)
    if match:
        year, month, day, hour, minute = (int(value) for value in match.groups())
        target = datetime(year, month, day, hour, minute, tzinfo=_shared.TZ)
        return (target, "none", "", f"{hour:02d}:{minute:02d}", prefix[match.end():])
    raise ValueError("时间格式支持：2026-11-20 18:00（单次）或 每周四 19:00（每周循环）")


def _require_group_admin(group_id: str, sender: str) -> list[dict] | None:
    if _admin_cmd.admin_scope(sender, group_id) is None:
        return [{"group_id": group_id, "text": "⚠️ 提醒中心仅限本群老师或超级管理员使用。"}]
    return None


def _split_reminder_command(args: str, now: datetime) -> tuple[datetime, str, str, str, str]:
    """把「时间 + 内容」拆开；时间前缀解析失败时抛出用法错误。"""
    raw = (args or "").strip()
    if not raw:
        raise ValueError("用法：/提醒添加 <时间> <内容>，例如 /提醒添加 每周四 19:00 交作业")
    target, rule, weekday, time_of_day, rest = parse_reminder_time(raw, now)
    content = rest.strip(" ，,、:：")
    return target, rule, weekday, time_of_day, content


def _summarize_reminder_content(raw_text: str) -> tuple[str, bool]:
    """自定义提醒内容复用统一提炼链路，超长时压缩成 <=60 字摘要。"""
    return _shared._prepare_requirement(raw_text, "提醒", category="提醒")


def cmd_reminder_add(group_id: str, sender: str, args: str) -> list[dict]:
    denied = _require_group_admin(group_id, sender)
    if denied:
        return denied
    now = _now_local()
    target, rule, weekday, time_of_day, content = _split_reminder_command(args, now)
    if not content:
        raise ValueError("用法：/提醒添加 <时间> <内容>，例如 /提醒添加 每周四 19:00 交作业")
    text, summarized = _summarize_reminder_content(content)
    reminder_id = db.create_custom_reminder(
        group_id, text, target.strftime("%Y-%m-%d %H:%M"),
        repeat_rule=rule, weekday=weekday, time_of_day=time_of_day, operator=sender,
    )
    repeat_label = "（每周重复）" if rule == "weekly" else ""
    reply = (
        f"已添加提醒 #{reminder_id}{repeat_label}：{text}\n"
        f"下次提醒：{target.strftime('%Y-%m-%d %H:%M')}"
    )
    if summarized:
        reply += "\n已自动提炼摘要，如需微调可用 /提醒修改。"
    return [{"group_id": group_id, "text": reply}]


def render_upcoming_reminders(group_id: str, now: datetime, days: int = 7) -> str:
    from cdutetc import reminder as reminder_module

    entries = _shared._upcoming_entries(group_id, now, days)
    if not entries:
        return f"未来 {days} 天暂无提醒。\n添加：/提醒添加 <时间> <内容>"
    lines = [f"未来 {days} 天提醒："]
    for index, entry in enumerate(entries[:20], 1):
        source = reminder_module.SOURCE_LABELS.get(entry["source"], entry["source"])
        status = reminder_module.STATUS_LABELS.get(entry["status"], entry["status"])
        repeat = "（每周）" if entry.get("repeat") == "weekly" else ""
        lines.append(
            f"{index}. [{source}]{entry['trigger'].strftime('%m-%d %H:%M')} "
            f"{entry['text']}{repeat}（{status}）"
        )
    if len(entries) > 20:
        lines.append(f"（共 {len(entries)} 条，仅显示前 20 条）")
    return "\n".join(lines)


def cmd_reminder_list(group_id: str, sender: str, args: str) -> list[dict]:
    """统一提醒清单：比赛 / 课程 / 自定义全部聚合，只读展示。"""
    denied = _require_group_admin(group_id, sender)
    if denied:
        return denied
    days = 7
    raw = (args or "").strip()
    if raw:
        try:
            days = int(raw.split()[0])
        except (TypeError, ValueError):
            raise ValueError("用法：/提醒列表 [天数]，默认 7 天") from None
        if not 1 <= days <= 30:
            raise ValueError("天数需在 1-30 之间")
    return [{"group_id": group_id, "text": render_upcoming_reminders(group_id, _now_local(), days)}]


def cmd_reminder_modify(group_id: str, sender: str, args: str) -> list[dict]:
    """支持改时间（含循环规则）或只改内容；序号与 /提醒列表 一致。"""
    denied = _require_group_admin(group_id, sender)
    if denied:
        return denied
    parts = (args or "").strip().split(maxsplit=1)
    if len(parts) < 2:
        raise ValueError("用法：/提醒修改 <序号> <新时间或新内容>")
    now = _now_local()
    existing = _shared._resolve_custom_reminder(group_id, parts[0], now)
    reminder_id = int(existing["id"])
    payload = parts[1].strip()
    try:
        target, rule, weekday, time_of_day, rest = parse_reminder_time(payload, now)
    except ValueError:
        text, summarized = _summarize_reminder_content(payload)
        db.update_custom_reminder(reminder_id, content=text, status="pending",
                                  operator=sender)
        reply = f"已更新提醒 #{reminder_id}：{text}"
        if summarized:
            reply += "\n已自动提炼摘要，如需微调可用 /提醒修改。"
        return [{"group_id": group_id, "text": reply}]
    raw_content = rest.strip(" ，,、:：")
    if raw_content:
        text, summarized = _summarize_reminder_content(raw_content)
    else:
        text, summarized = (existing.get("content") or ""), False
    db.update_custom_reminder(reminder_id, target_time=target.strftime("%Y-%m-%d %H:%M"),
                              content=text, status="pending", operator=sender)
    db.set_custom_reminder_repeat(reminder_id, repeat_rule=rule, weekday=weekday,
                                  time_of_day=time_of_day, operator=sender)
    repeat_label = "（每周重复）" if rule == "weekly" else ""
    reply = (
        f"已更新提醒 #{reminder_id}{repeat_label}：{text}\n"
        f"下次提醒：{target.strftime('%Y-%m-%d %H:%M')}"
    )
    if summarized:
        reply += "\n已自动提炼摘要，如需微调可用 /提醒修改。"
    return [{"group_id": group_id, "text": reply}]


def cmd_reminder_delete(group_id: str, sender: str, args: str) -> list[dict]:
    denied = _require_group_admin(group_id, sender)
    if denied:
        return denied
    raw = (args or "").strip()
    if not raw:
        raise ValueError("用法：/提醒删除 <序号>，序号取自 /提醒列表")
    existing = _shared._resolve_custom_reminder(group_id, raw.split()[0], _now_local())
    reminder_id = int(existing["id"])
    db.delete_custom_reminder(reminder_id, sender)
    return [{"group_id": group_id,
             "text": f"已删除提醒 #{reminder_id}：{existing.get('content') or ''}"}]
