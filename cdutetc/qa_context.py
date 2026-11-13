"""问答链路的动态群上下文（只读聚合）。

`@机器人 提问`（未带 / 命令）时，把本群最近 7 天的课程、进行中的征集、
已录入 FAQ 拼成一段精简事实块，交给 :func:`cdutetc.llm.answer_question`
注入 system prompt。模型只按这些事实作答，被问到操作时只能引导白名单命令，
避免关键词硬匹配踩空后退化成「未收录」。

本模块严格只读：不写库、不发信、不建定时节点；任何一路数据源异常都只降级
为「该段为空」，不影响问答主链路。
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

from cdutetc import config, daily_digest
from cdutetc import database as db
from cdutetc.daily_digest import TZ, _clock, _load_fields, _now, _training_weekday
from cdutetc.utils import normalize_time_expression, split_normalized_time

logger = logging.getLogger(__name__)

CONTEXT_TITLE = "【当前集训队最新教务事实】"
# 课程窗口：未来 7 天（含今天），与「近期课程」的语义一致。
SESSION_WINDOW_DAYS = 7
MAX_SESSION_ITEMS = 14
MAX_COLLECTION_ITEMS = 8
MAX_FAQ_ITEMS = 30
# 单条 FAQ 的展示上限，避免个别长答案挤占整段上下文预算。
FAQ_QUESTION_CHARS = 60
FAQ_ANSWER_CHARS = 120

_WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 引导白名单：只允许模型建议这些真实存在的命令，杜绝臆造。
COMMAND_GUIDE = (
    "/课程列表、/校安排、/征集汇总、/报名 <征集ID> 姓名=xxx",
    "/FAQ列表、/比赛状态、/提醒状态、/帮助",
)


# ---------------------------------------------------------------------
# 数据源收集（只读，逐路降级）
# ---------------------------------------------------------------------

def week_sessions(group_id: str, now: datetime | None = None,
                  days: int = SESSION_WINDOW_DAYS) -> list[dict]:
    """未来 ``days`` 天内（含今天）的课程：星期匹配、未取消、且尚未结束。"""
    current = _now(now)
    gid = (group_id or "").strip()
    if not gid or days <= 0:
        return []
    try:
        sessions = db.list_training_sessions(gid, active_only=False)
    except Exception:  # noqa: BLE001 - 读库异常不应阻断问答
        logger.warning("问答上下文读取课程失败，按无课程处理 [group=%s]", gid, exc_info=True)
        return []

    out: list[dict] = []
    for offset in range(days):
        day = current.date() + timedelta(days=offset)
        for session in sessions:
            if _training_weekday(session.get("weekday")) != day.weekday():
                continue
            start_clock = _clock(session.get("start_time"))
            if start_clock is None:
                continue
            start = datetime.combine(day, start_clock, tzinfo=TZ)
            end_clock = _clock(session.get("end_time"))
            end = datetime.combine(day, end_clock, tzinfo=TZ) if end_clock else start
            if end < start:  # 跨零点课程：结束时间视为次日
                end += timedelta(days=1)
            if end <= current:
                continue
            try:
                if not db.training_occurrence_valid(session, start):
                    continue
            except Exception:  # noqa: BLE001 - 生效期校验失败按无效处理
                logger.warning("课程生效期校验失败 [session=%s]", session.get("id"), exc_info=True)
                continue
            out.append({
                "id": session.get("id"),
                "title": session.get("title") or "训练安排",
                "weekday_label": _WEEKDAY_LABELS[day.weekday()],
                "date_label": day.strftime("%m-%d") + " " + _WEEKDAY_LABELS[day.weekday()],
                "start_text": start.strftime("%H:%M"),
                "end_text": end.strftime("%H:%M") if end_clock else "",
                "location": (session.get("location") or "").strip(),
                "start_dt": start,
            })
    out.sort(key=lambda item: item["start_dt"])
    return out[:MAX_SESSION_ITEMS]


def weekly_courses(group_id: str, now: datetime | None = None) -> list[dict]:
    """本周（``now`` 所在周，周一~周日）可上的周常课程，按星期与开始时间排序。

    与 :func:`week_sessions` 的区别：这里不剪掉「本周已过去」的课，而是把每门课
    落到本周对应的公历日期上，模型据此才能回答「这周四有课吗」而不是回答「未登记」。
    """
    current = _now(now)
    gid = (group_id or "").strip()
    if not gid:
        return []
    try:
        sessions = db.list_training_sessions(gid, active_only=False)
    except Exception:  # noqa: BLE001 - 读库异常不应阻断问答
        logger.warning("问答上下文读取周常课程失败 [group=%s]", gid, exc_info=True)
        return []
    monday = current.date() - timedelta(days=current.weekday())
    out: list[dict] = []
    for session in sessions:
        if (session.get("status") or "confirmed") != "confirmed":
            continue
        weekday_index = _training_weekday(session.get("weekday"))
        if weekday_index is None:
            continue
        start_clock, end_clock = _normalized_clocks(session.get("start_time"), session.get("end_time"))
        if start_clock is None:
            continue
        day = monday + timedelta(days=weekday_index)
        out.append({
            "id": session.get("id"),
            "title": session.get("title") or "训练安排",
            "weekday_label": _WEEKDAY_LABELS[weekday_index],
            "start_text": start_clock.strftime("%H:%M"),
            "end_text": end_clock.strftime("%H:%M") if end_clock else "",
            "date_text": day.strftime("%Y-%m-%d"),
            "location": (session.get("location") or "").strip(),
            "sort_key": (weekday_index, start_clock),
        })
    out.sort(key=lambda item: item["sort_key"])
    return out[:MAX_SESSION_ITEMS]


def open_collections(group_id: str) -> list[dict]:
    """状态为 open 的征集：标题、ID、截止时间、剩余名额、登记字段。"""
    gid = (group_id or "").strip()
    if not gid:
        return []
    try:
        collections = db.list_collections(gid, status="open")
    except Exception:  # noqa: BLE001
        logger.warning("问答上下文读取征集失败，按无征集处理 [group=%s]", gid, exc_info=True)
        return []
    out: list[dict] = []
    for item in collections:
        remaining = None
        try:
            remaining = db.summarize_collection(item["id"]).get("remaining")
        except Exception:  # noqa: BLE001 - 名额统计失败不影响列出征集
            logger.warning("问答上下文统计名额失败 [collection=%s]", item.get("id"), exc_info=True)
        out.append({
            "id": item.get("id"),
            "title": item.get("title") or "征集",
            "deadline": (item.get("deadline") or "").strip(),
            "remaining": remaining,
            "fields": _load_fields(item.get("fields_json")),
        })
    return out[:MAX_COLLECTION_ITEMS]


def faq_pairs(group_id: str, limit: int = MAX_FAQ_ITEMS) -> list[dict]:
    """本群已录入的 FAQ 问答对（不含已删除）。"""
    gid = (group_id or "").strip()
    if not gid or limit <= 0:
        return []
    try:
        items = db.list_faq(gid)
    except Exception:  # noqa: BLE001
        logger.warning("问答上下文读取 FAQ 失败，按无 FAQ 处理 [group=%s]", gid, exc_info=True)
        return []
    out: list[dict] = []
    for item in items[:limit]:
        question = (item.get("question") or "").strip()
        answer = (item.get("answer") or "").strip()
        if not question or not answer:
            continue
        out.append({"question": question, "answer": answer})
    return out


# ---------------------------------------------------------------------
# 文本结构化
# ---------------------------------------------------------------------

def _session_line(item: dict) -> str:
    span = item["start_text"] if not item["end_text"] else f'{item["start_text"]}-{item["end_text"]}'
    line = f'- {item["date_label"]} {span} {item["title"]}'
    if item["location"]:
        line += f'（{item["location"]}）'
    return line


def _normalized_clocks(start_raw: str, end_raw: str) -> tuple[time | None, time | None]:
    """把脏时间（如 ``19-21点``）先归一化再解析，归一化失败退回原始写法。"""
    packed = " ".join(x for x in ((start_raw or "").strip(), (end_raw or "").strip()) if x)
    _weekday, start_text, end_text = split_normalized_time(normalize_time_expression(packed))
    start_clock = _clock(start_text) or _clock(start_raw)
    end_clock = _clock(end_text) or _clock(end_raw)
    return start_clock, end_clock


def _span_text(item: dict) -> str:
    """课程时间段文本；展示前统一走一遍归一化，保证脏数据显示为 ``HH:MM``。"""
    span = item["start_text"] if not item["end_text"] else f'{item["start_text"]}-{item["end_text"]}'
    return normalize_time_expression(span) or span


def _week_course_line(item: dict) -> str:
    """段落 1 行：显式带本周公历日期，避免模型把周常课程判为「未登记」。"""
    return (f'- {item["title"]}｜{item["weekday_label"]} {_span_text(item)}'
            f'｜本周具体日期 {item["date_text"]}｜{item["location"] or "地点待定"}')


def _repeat_course_line(item: dict) -> str:
    """段落 4 行：周常重复规则，不含单次日期。"""
    label = item["weekday_label"]
    if label.startswith("周"):
        label = label[1:]
    return (f'- {item["title"]}｜每周{label} {_span_text(item)}'
            f'｜{item["location"] or "地点待定"}')


def _collection_line(item: dict) -> str:
    line = f'- [ID: {item["id"]}] {item["title"]}'
    details: list[str] = []
    if item["deadline"]:
        details.append(f'截止 {item["deadline"]}')
    if item["remaining"] is not None:
        details.append(f'剩 {item["remaining"]} 个名额')
    if item["fields"]:
        details.append("字段: " + "、".join(item["fields"]))
    if details:
        line += "（" + "，".join(details) + "）"
    return line


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _truncate(text: str, limit: int) -> str:
    """按整行截断，保留标题与结构；超限时补省略标记。"""
    if limit <= 0 or len(text) <= limit:
        return text
    kept: list[str] = []
    total = 0
    for line in text.splitlines():
        if total + len(line) + 1 > limit - 12:
            break
        kept.append(line)
        total += len(line) + 1
    kept.append("…（上下文已截断，细节请用斜杠命令查询）")
    return "\n".join(kept)


def build_group_qa_context(group_id: str, now: datetime | None = None) -> str:
    """组装本群问答事实上下文；三路数据全空或开关关闭时返回空串。

    返回示例::

        【当前集训队最新教务事实】
        1. 近期课程（未来 7 天）：
        - 11-11 周三 19:00-21:00 图论进阶（实训楼 B302）
        2. 进行中的征集与报名：
        - [ID: 3] 天梯赛选拔（截止 2026-11-12 12:00，剩 2 个名额，字段: 姓名、学号）
        3. 常见问题（FAQ）：
        - Q: 机房钥匙找谁？ A: 找当天值日助教或负责人。
    """
    if not getattr(config, "QA_CONTEXT_ENABLED", True):
        return ""
    gid = (group_id or "").strip()
    if not gid:
        return ""

    current = _now(now)
    weekly = weekly_courses(gid, now)
    collections = open_collections(gid)
    faqs = faq_pairs(gid)
    if not (weekly or collections or faqs):
        return ""

    monday = current.date() - timedelta(days=current.weekday())
    lines = [CONTEXT_TITLE]
    # 先钉死本周的日期锚点：模型拿到具体公历日期才不会把「周四」算错一天。
    lines.append(
        f"本周周一为 {monday.strftime('%Y-%m-%d')}，"
        f"今天 {current.date().strftime('%Y-%m-%d')}（{_WEEKDAY_LABELS[current.weekday()]}）。"
    )
    lines.append(f"1. 近期课程（本周排期，未来 {SESSION_WINDOW_DAYS} 天）：")
    lines.extend([_week_course_line(item) for item in weekly] or ["- 暂无已登记课程"])
    lines.append("2. 进行中的征集与报名：")
    lines.extend([_collection_line(item) for item in collections] or ["- 暂无开放中的征集"])
    lines.append("3. 常见问题（FAQ）：")
    lines.extend(
        [f'- Q: {_clip(item["question"], FAQ_QUESTION_CHARS)} A: {_clip(item["answer"], FAQ_ANSWER_CHARS)}'
         for item in faqs] or ["- 暂无已录入 FAQ"]
    )
    lines.append("4. 每周常规训练/任务（每周重复，无单次日期）：")
    lines.extend([_repeat_course_line(item) for item in weekly] or ["- 暂无周常课程"])
    return _truncate(
        "\n".join(lines),
        int(getattr(config, "QA_CONTEXT_MAX_CHARS", 3000)),
    )


__all__ = [
    "CONTEXT_TITLE",
    "COMMAND_GUIDE",
    "build_group_qa_context",
    "week_sessions",
    "weekly_courses",
    "open_collections",
    "faq_pairs",
]

# 保留 daily_digest 引用，表明课程解析逻辑与其同源（避免重复实现）。
_ = daily_digest
