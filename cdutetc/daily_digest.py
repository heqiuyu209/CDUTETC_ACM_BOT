"""每日聚合晨报与被动搭便车（piggyback）内容构建。

官方 QQ 协议单群主动消息额度极低（1~4 条/天），因此提醒机制升级为双轨：

- **当日总览晨报**：每群每天最多 1 条主动消息，逐群独立生成「今日课程 +
  今日开赛 + 今日其他安排」，只讲当天的事，跨群内容互不串味；
- **被动搭便车**：群员只读查询（/课程列表、/FAQ列表、/帮助、/比赛状态……）
  触发被动回复时，在尾部追加一条最紧急的待办，零消耗主动额度。

设计边界：
- 本模块**只读**（读库、读预检、不写库、不发信、不登记投递），可被定时任务、
  命令层与单测任意调用；
- 只产出**纯文本**，不使用 Markdown 语法（避免官方客户端拦截或渲染错乱）；
- 三段皆空时返回 ``None``，绝不发空消息浪费宝贵的主动额度；
- 时间口径与提醒链路共用 :func:`cdutetc.reminder._parse_time`，避免漂移。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from cdutetc import config
from cdutetc import database as db
from cdutetc.reminder import _parse_time
from cdutetc.utils import normalize_time_expression

TZ = ZoneInfo(config.TZ_NAME)
logger = logging.getLogger(__name__)

# 晨报收集窗口：48 小时内截止的征集、24 小时内开赛且预检就绪的比赛。
COLLECTION_SOON_HOURS = 48.0
CONTEST_SOON_HOURS = 24.0
# 搭便车「最紧急」阈值：12 小时内截止报名、2 小时内开赛。
PIGGYBACK_COLLECTION_HOURS = 12.0
PIGGYBACK_CONTEST_HOURS = 2.0

DIGEST_TITLE = "【CDUTETC ACM 今日事务总览】"
DIGEST_RULE = "-" * 24
DIGEST_FOOTER = "发送对应命令（如 /课程列表、/报名 <id>）查看详情"

_WEEKDAY_NAMES = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def _now(now: datetime | None = None) -> datetime:
    """统一为带北京时区的当前时间；注入的无时区时间按北京时间解释。"""
    value = now or datetime.now(TZ)
    return value.replace(tzinfo=TZ) if value.tzinfo is None else value.astimezone(TZ)


def _training_weekday(text: str) -> int | None:
    """解析星期文本（周一/星期一/礼拜一/每周三，或提醒表里的裸「三」）。"""
    value = (text or "").strip()
    if value.startswith("每"):
        value = value[1:]
    if value in _WEEKDAY_NAMES:  # custom_reminders.weekday 只存单个汉字
        return _WEEKDAY_NAMES[value]
    for prefix in ("星期", "礼拜", "周"):
        if value.startswith(prefix):
            return _WEEKDAY_NAMES.get(value[len(prefix):])
    return None


def _clock(text: str) -> time | None:
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime((text or "").strip(), fmt).time()
        except (ValueError, TypeError):
            continue
    return None


def _remaining_label(delta: timedelta) -> str:
    """把剩余时间渲染成「2 小时 15 分钟」这类中文短标签。"""
    seconds = int(max(0.0, delta.total_seconds()))
    if seconds < 60:
        return "不到 1 分钟"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, remainder = divmod(minutes, 60)
    return f"{hours} 小时" + (f" {remainder} 分钟" if remainder else "")


# ---------------------------------------------------------------------
# 数据源收集（只读）
# ---------------------------------------------------------------------

def today_sessions(group_id: str, now: datetime | None = None) -> list[dict]:
    """今日课程：星期匹配、未取消、且尚未结束的训练安排。"""
    current = _now(now)
    gid = (group_id or "").strip()
    if not gid:
        return []
    out: list[dict] = []
    try:
        sessions = db.list_training_sessions(gid, active_only=False)
    except Exception:  # noqa: BLE001 - 读库异常不应让整份日报失败
        logger.warning("晨报读取课程失败，按无课程处理 [group=%s]", gid, exc_info=True)
        return []
    for session in sessions:
        if _training_weekday(session.get("weekday")) != current.weekday():
            continue
        start_clock = _clock(session.get("start_time"))
        if start_clock is None:
            continue
        start = datetime.combine(current.date(), start_clock, tzinfo=TZ)
        if not db.training_occurrence_valid(session, start):
            continue
        end_clock = _clock(session.get("end_time"))
        end = datetime.combine(current.date(), end_clock, tzinfo=TZ) if end_clock else start
        if end < start:  # 跨零点课程：结束时间视为次日
            end += timedelta(days=1)
        if end_clock is not None and end <= current:
            continue  # 已结束
        out.append({
            "id": session.get("id"),
            "title": session.get("title") or "训练安排",
            "start_text": start.strftime("%H:%M"),
            "end_text": end.strftime("%H:%M") if end_clock else "",
            "location": (session.get("location") or "").strip(),
            "requirement": (session.get("requirement") or "").strip(),
            "ongoing": start <= current,
            "start_dt": start,
        })
    out.sort(key=lambda item: item["start_dt"])
    return out


def soon_collections(group_id: str, now: datetime | None = None,
                     hours: float = COLLECTION_SOON_HOURS) -> list[dict]:
    """临近截止征集：状态 open 且距截止不足 ``hours`` 小时（已过期的不算）。"""
    current = _now(now)
    gid = (group_id or "").strip()
    if not gid or hours <= 0:
        return []
    try:
        collections = db.list_collections(gid, status="open")
    except Exception:  # noqa: BLE001
        logger.warning("晨报读取征集失败，按无征集处理 [group=%s]", gid, exc_info=True)
        return []
    out: list[dict] = []
    for item in collections:
        deadline = _parse_time(item.get("deadline") or "")
        if deadline is None:
            continue
        delta = deadline - current
        if delta <= timedelta(0) or delta > timedelta(hours=hours):
            continue
        remaining = None
        try:
            summary = db.summarize_collection(item["id"])
            remaining = summary.get("remaining")
        except Exception:  # noqa: BLE001 - 名额统计失败不影响列出征集
            logger.warning("征集名额统计失败 [collection=%s]", item.get("id"), exc_info=True)
        out.append({
            "id": item.get("id"),
            "title": item.get("title") or "征集",
            "deadline": item.get("deadline") or "",
            "deadline_dt": deadline,
            "remaining": remaining,
            "fields": _load_fields(item.get("fields_json")),
        })
    out.sort(key=lambda item: item["deadline_dt"])
    return out


def soon_competitions(group_id: str, now: datetime | None = None,
                      hours: float = CONTEST_SOON_HOURS, *,
                      require_delivery_ready: bool = True) -> list[dict]:
    """列出未来指定时间内本群可见的赛事。

    晨报要求主动投递预检通过；被动提示不依赖主动投递状态。
    """
    current = _now(now)
    gid = (group_id or "").strip()
    if not gid or hours <= 0:
        return []
    try:
        competitions = db.list_group_competitions(gid)
    except Exception:  # noqa: BLE001
        logger.warning("晨报读取比赛失败，按无赛事处理 [group=%s]", gid, exc_info=True)
        return []
    from cdutetc import reminder_preflight

    out: list[dict] = []
    for contest in competitions:
        start = _parse_time(contest.get("official_time") or "")
        if start is None:
            continue
        delta = start - current
        if delta <= timedelta(0) or delta > timedelta(hours=hours):
            continue
        try:
            if require_delivery_ready:
                ready, _ = reminder_preflight.check_contest_delivery_readiness(contest["id"], gid, current)
                if not ready:
                    continue
            elif db.is_weekly_competition(contest["id"]):
                if gid not in db.get_weekly_groups():
                    continue
                if config.WEEKLY_CONTEST_GROUPS and gid not in config.WEEKLY_CONTEST_GROUPS:
                    continue
        except Exception:  # noqa: BLE001 - 无法确认可见性或投递状态时跳过
            logger.warning("比赛状态检查异常，跳过 [contest=%s]", contest.get("id"), exc_info=True)
            continue
        out.append({
            "id": contest.get("id"),
            "name": contest.get("name") or "比赛",
            "start": contest.get("official_time") or "",
            "start_dt": start,
            "site": (contest.get("official_site") or "").strip(),
        })
    out.sort(key=lambda item: item["start_dt"])
    return out


def _weekly_visible_to(group_id: str) -> bool:
    """周赛只对本群「周赛订阅群」可见：库内订阅为准，配置文件再兜一层。"""
    if group_id not in db.get_weekly_groups():
        return False
    if config.WEEKLY_CONTEST_GROUPS and group_id not in config.WEEKLY_CONTEST_GROUPS:
        return False
    return True


def today_competitions(group_id: str, now: datetime | None = None) -> list[dict]:
    """今日开赛的赛事：official_time 的日期必须正好是今天。

    普通入库比赛（CF / 牛客等）逐群复检主动投递就绪状态，避免把已挂起、
    被取消或已投递过的场次再塞进晨报；队内周赛只在**周日**且本群确属
    周赛订阅群时出现，周一到周六一律不出现，杜绝「周五晨报报周日周赛」。
    """
    current = _now(now)
    gid = (group_id or "").strip()
    if not gid:
        return []
    try:
        competitions = db.list_group_competitions(gid)
    except Exception:  # noqa: BLE001 - 读库异常按无赛事处理
        logger.warning("晨报读取比赛失败，按无比赛处理 [group=%s]", gid, exc_info=True)
        return []
    from cdutetc import reminder_preflight

    out: list[dict] = []
    for contest in competitions:
        start = _parse_time(contest.get("official_time") or "")
        if start is None or start.date() != current.date():
            continue
        try:
            weekly = db.is_weekly_competition(contest["id"])
            if weekly:
                if current.weekday() != 6:  # 队内周赛固定在周日开赛
                    continue
                if not _weekly_visible_to(gid):
                    continue
            else:
                ready, _ = reminder_preflight.check_contest_delivery_readiness(
                    contest["id"], gid, current
                )
                if not ready:
                    continue
        except Exception:  # noqa: BLE001 - 无法确认可见性时跳过
            logger.warning("比赛状态检查异常，跳过 [contest=%s]", contest.get("id"), exc_info=True)
            continue
        out.append({
            "id": contest.get("id"),
            "name": contest.get("name") or "比赛",
            "start": contest.get("official_time") or "",
            "start_dt": start,
            "site": (contest.get("official_site") or "").strip(),
            "weekly": weekly,
        })
    out.sort(key=lambda item: item["start_dt"])
    return out


def today_reminders(group_id: str, now: datetime | None = None) -> list[dict]:
    """今日其他安排：本群今天触发的自定义提醒（单次命中日期 / 每周命中星期）。

    自定义提醒由老师按群维护，不走群提醒开关，因此不受三段开关抑制。
    """
    current = _now(now)
    gid = (group_id or "").strip()
    if not gid:
        return []
    try:
        # 只看待办：已发送（done）或取消（cancelled）的不该再出现在今日总览里。
        reminders = db.list_custom_reminders(gid, statuses=("pending",))
    except Exception:  # noqa: BLE001 - 读库异常按无安排处理
        logger.warning("晨报读取自定义提醒失败，按无安排处理 [group=%s]", gid, exc_info=True)
        return []
    out: list[dict] = []
    for item in reminders:
        rule = (item.get("repeat_rule") or "none").strip() or "none"
        target = _parse_time(item.get("target_time") or "")
        if rule == "weekly":
            if _training_weekday(item.get("weekday")) != current.weekday():
                continue
            clock = _clock(item.get("time_of_day") or "")
            if clock is None and target is not None:
                clock = target.time()
            if clock is None:
                continue
            trigger = datetime.combine(current.date(), clock, tzinfo=TZ)
        else:
            if target is None or target.date() != current.date():
                continue
            trigger = target
        out.append({
            "id": item.get("id"),
            "content": (item.get("content") or "").strip() or "提醒",
            "time_text": trigger.strftime("%H:%M"),
            "repeat": rule,
            "trigger_dt": trigger,
        })
    out.sort(key=lambda item: item["trigger_dt"])
    return out


def _load_fields(raw) -> list[str]:
    """征集的登记字段（JSON 字符串），解析失败按空列表处理。"""
    if not raw:
        return []
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


# ---------------------------------------------------------------------
# 排版
# ---------------------------------------------------------------------

def render_digest(sessions: list[dict], reminders: list[dict], competitions: list[dict],
                  now: datetime | None = None) -> str | None:
    """把当日三段（课程 / 赛事 / 其他安排）渲染成单条纯文本晨报；全空返回 None。"""
    if not (sessions or reminders or competitions):
        return None
    current = _now(now)
    lines = [DIGEST_TITLE]

    if sessions:
        lines.append("📚 今日课程/训练：")
        for item in sessions:
            span = (item["start_text"] if not item.get("end_text")
                    else f"{item['start_text']}-{item['end_text']}")
            span = normalize_time_expression(span) or span
            # 带上具体公历日期：跨天追问「今天 / 明天有课吗」时模型不会算错日子。
            start_dt = item.get("start_dt")
            date_text = start_dt.strftime("%Y-%m-%d") if start_dt else ""
            line = f"- {date_text} {span} {item['title']}".replace("-  ", "- ")
            # 地点缺失（None / 空串 / 纯空白）统一显示「地点：待定」，
            # 括号本身不省略，群员不会误以为是漏排版。
            location = (item.get("location") or "").strip() or "待定"
            tail = f"地点：{location}"
            requirement = (item.get("requirement") or "").strip()
            if requirement:
                tail += f"｜要求：{requirement}"
            line += f" ({tail})"
            if item.get("ongoing"):
                line += "（进行中）"
            lines.append(line)

    if competitions:
        lines.append("🏆 今日赛事：")
        for item in competitions:
            delta = item["start_dt"] - current
            when = ("进行中" if delta <= timedelta(0)
                    else f"{_remaining_label(delta)}后开赛")
            lines.append(f"- {item['name']}（{item['start']} 开赛，{when}）")

    if reminders:
        lines.append("🗓 今日其他安排：")
        for item in reminders:
            suffix = "（每周）" if item.get("repeat") == "weekly" else ""
            lines.append(f"- {item['time_text']} {item['content']}{suffix}")

    lines.append(DIGEST_RULE)
    lines.append(DIGEST_FOOTER)
    return "\n".join(lines)


def build_daily_digest(group_id: str, now: datetime | None = None) -> str | None:
    """构建某群的「当日总览」；三段皆空时返回 None（不发空消息、不占主动额度）。

    逐群独立生成：课程段与赛事段各自受本群开关
    （``course_remind_enabled`` / ``contest_remind_enabled``）控制，
    自定义提醒段照常展示今日触发的安排。

    Args:
        group_id: 目标群标识。
        now: 当前时间（北京时区）；默认取系统当前时间。

    Returns:
        纯文本晨报正文，或 None。
    """
    current = _now(now)
    gid = (group_id or "").strip()
    if not gid:
        return None
    sessions: list[dict] = []
    competitions: list[dict] = []
    if db.group_switch_enabled(gid, "course_remind_enabled"):
        sessions = today_sessions(gid, current)
    if db.group_switch_enabled(gid, "contest_remind_enabled"):
        competitions = today_competitions(gid, current)
    # 自定义提醒属于老师手工维护的事务，不受上面两个群开关约束。
    reminders = today_reminders(gid, current)
    return render_digest(sessions, reminders, competitions, current)


def build_piggyback_notice(group_id: str, now: datetime | None = None) -> str | None:
    """被动回复可尾随的最紧急待办（12 小时内截止报名 / 2 小时内开赛）。

    多个候选时取时间上最紧的一条；无候选返回 None。
    """
    current = _now(now)
    gid = (group_id or "").strip()
    if not gid:
        return None
    candidates: list[tuple[datetime, str, dict]] = []
    for item in soon_collections(gid, current, PIGGYBACK_COLLECTION_HOURS):
        candidates.append((item["deadline_dt"], "collection", item))
    # 被动提示不依赖主动投递预检，但仍须尊重本群「比赛提醒」开关：
    # 群关闭比赛提醒后，搭车文案里也不该再出现赛事。
    if db.group_switch_enabled(gid, "contest_remind_enabled"):
        for item in soon_competitions(gid, current, PIGGYBACK_CONTEST_HOURS,
                                      require_delivery_ready=False):
            candidates.append((item["start_dt"], "contest", item))
    if not candidates:
        return None
    target, kind, item = min(candidates, key=lambda candidate: candidate[0])
    if kind == "collection":
        command = f"/报名 {item['id']}"
        if item.get("fields"):
            command += f" {item['fields'][0]}=..."
        line = (f"💡 事务速递：「{item['title']}」"
                f"{_remaining_label(target - current)}后截止，回复「{command}」快捷登记。")
    else:
        line = (f"💡 事务速递：「{item['name']}」"
                f"{_remaining_label(target - current)}后开赛，记得提前登录并检查环境。")
    return "---\n" + line
