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
import re
from datetime import datetime, time, timedelta

from cdutetc import config, content_filter, daily_digest
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
# @提问时只注入与本问最相关的前 N 条 FAQ：全量塞进去会稀释事实、也挤占 token。
FAQ_TOP_N = 3
# 单条 FAQ 的展示上限，避免个别长答案挤占整段上下文预算。
FAQ_QUESTION_CHARS = 60
FAQ_ANSWER_CHARS = 120
COMPACT_FACT_TOKEN_LIMIT = 160
COMPACT_FACT_MAX_ITEMS = 3

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
    except Exception:
        logger.warning("问答上下文读取课程失败，按无课程处理 [group=%s]", gid, exc_info=True)
        return []

    out: list[dict] = []
    for offset in range(days):
        day = current.date() + timedelta(days=offset)
        for session in sessions:
            if _training_weekday(session.get("weekday")) != day.weekday():
                continue
            start_clock, end_clock = _normalized_clocks(session.get("start_time"), session.get("end_time"))
            if start_clock is None:
                continue
            start = datetime.combine(day, start_clock, tzinfo=TZ)
            end = datetime.combine(day, end_clock, tzinfo=TZ) if end_clock else start
            if end < start:  # 跨零点课程：结束时间视为次日
                end += timedelta(days=1)
            if end <= current:
                continue
            try:
                if not _valid_session_occurrence(session, start):
                    continue
            except Exception:
                logger.warning("课程生效期校验失败 [session=%s]", session.get("id"), exc_info=True)
                continue
            out.append({
                "id": session.get("id"),
                "title": session.get("title") or "训练安排",
                "weekday_label": _WEEKDAY_LABELS[day.weekday()],
                "date_label": day.strftime("%m-%d") + " " + _WEEKDAY_LABELS[day.weekday()],
                "date_text": day.strftime("%Y-%m-%d"),
                "start_text": start.strftime("%H:%M"),
                "end_text": end.strftime("%H:%M") if end_clock else "",
                "location": (session.get("location") or "").strip(),
                "start_dt": start,
                "recurring": not _single_day_session(session),
            })
    out.sort(key=lambda item: item["start_dt"])
    return out[:MAX_SESSION_ITEMS]


def weekly_courses(group_id: str, now: datetime | None = None) -> list[dict]:
    """本周（周一~周日）实际有效的课程实例，区分单次和每周安排。

    与 :func:`week_sessions` 的区别：这里不剪掉「本周已过去」的课，而是把每门课
    落到本周对应的公历日期上，模型据此才能回答「这周四有课吗」而不是回答「未登记」。
    """
    current = _now(now)
    gid = (group_id or "").strip()
    if not gid:
        return []
    try:
        sessions = db.list_training_sessions(gid, active_only=False)
    except Exception:
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
        start = datetime.combine(day, start_clock, tzinfo=TZ)
        if not _valid_session_occurrence(session, start):
            continue
        out.append({
            "id": session.get("id"),
            "title": session.get("title") or "训练安排",
            "weekday_label": _WEEKDAY_LABELS[weekday_index],
            "start_text": start_clock.strftime("%H:%M"),
            "end_text": end_clock.strftime("%H:%M") if end_clock else "",
            "date_text": day.strftime("%Y-%m-%d"),
            "location": (session.get("location") or "").strip(),
            "sort_key": (weekday_index, start_clock),
            "recurring": not _single_day_session(session),
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
    except Exception:
        logger.warning("问答上下文读取征集失败，按无征集处理 [group=%s]", gid, exc_info=True)
        return []
    out: list[dict] = []
    for item in collections:
        remaining = None
        try:
            remaining = db.summarize_collection(item["id"]).get("remaining")
        except Exception:
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
    except Exception:
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


def relevant_faqs(group_id: str, question: str, limit: int = FAQ_TOP_N) -> list[dict]:
    """与当前提问最相关的已生效 FAQ（按 ``cdutetc.faq`` 的打分取 Top-N）。

    只取 confirmed 条目：``db.list_faq`` 的默认口径就是 confirmed，
    待确认草稿（pending）绝不能被当作教务事实注入提示词。
    """
    gid = (group_id or "").strip()
    text = (question or "").strip()
    if not gid or not text or limit <= 0:
        return []
    try:
        items = db.list_faq(gid)
    except Exception:
        logger.warning("问答上下文读取相关 FAQ 失败 [group=%s]", gid, exc_info=True)
        return []
    if not items:
        return []
    # 延迟导入：faq 模块本身不依赖 qa_context，放在函数内可避免导入期耦合。
    from cdutetc import faq as faq_search

    out: list[dict] = []
    for score, item in faq_search.top_matches(text, items, limit=limit):
        question_text = (item.get("question") or "").strip()
        answer = (item.get("answer") or "").strip()
        if not question_text or not answer:
            continue
        out.append({
            "id": item.get("id"),
            "question": question_text,
            "answer": answer,
            "score": round(score, 3),
        })
    return out


# ---------------------------------------------------------------------
# 文本结构化
# ---------------------------------------------------------------------


def _valid_session_occurrence(session: dict, start: datetime) -> bool:
    """规范化已有时间再校验真实生效期；未知值不能生成课程实例。"""
    index = _training_weekday(session.get("weekday"))
    if index is None:
        return False
    normalized = {**session, "weekday": _WEEKDAY_LABELS[index],
                  "start_time": start.strftime("%H:%M:%S")}
    try:
        return db.training_occurrence_valid(normalized, start)
    except Exception:
        logger.warning("问答课程实例校验失败 [session=%s]", session.get("id"), exc_info=True)
        return False


def _single_day_session(session: dict) -> bool:
    start = db._delivery_parse_time(session.get("effective_start") or "")
    end = db._delivery_parse_time(session.get("effective_end") or "")
    return bool(start and end and start <= end and end - start < timedelta(days=7))


def _single_occurrence_date(session: dict) -> str:
    """短有效期的实际开课日，不能把有效期第一天冒认为开课日。"""
    if not _single_day_session(session):
        return ""
    bound = db._delivery_parse_time(session.get("effective_start") or "")
    weekday = _training_weekday(session.get("weekday"))
    start_clock, _ = _normalized_clocks(session.get("start_time"), session.get("end_time"))
    if weekday is None or start_clock is None:
        return ""
    day = bound.date() + timedelta(days=(weekday - bound.weekday()) % 7)
    occurrence = datetime.combine(day, start_clock, tzinfo=TZ)
    if occurrence < bound:
        occurrence += timedelta(days=7)
    return occurrence.date().isoformat() if _valid_session_occurrence(session, occurrence) else ""


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
    kind = "｜单次安排" if not item.get("recurring", True) else ""
    return (f'- {item["title"]}｜{item["weekday_label"]} {_span_text(item)}'
            f'｜本周具体日期 {item["date_text"]}｜{item["location"] or "地点待定"}{kind}')


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


def _faq_line(item: dict) -> str:
    """FAQ 行：带编号，模型据此才能给出「依据 FAQ #3」这类来源提示。"""
    head = f'#{item["id"]} ' if item.get("id") else ""
    return (f'- {head}Q: {_clip(item["question"], FAQ_QUESTION_CHARS)}'
            f' A: {_clip(item["answer"], FAQ_ANSWER_CHARS)}')


def build_group_qa_context(group_id: str, now: datetime | None = None,
                           question: str = "") -> str:
    """组装本群问答事实上下文；三路数据全空或开关关闭时返回空串。

    ``question`` 非空时（@机器人 提问场景），FAQ 段只注入与该问题最相关的
    ``FAQ_TOP_N`` 条，让模型聚焦证据、避免分工不明的泛化条目干扰作答。
    ``question`` 留空时保持原行为（全量 ``MAX_FAQ_ITEMS`` 条），供摘要类场景复用。

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
    asked = (question or "").strip()
    weekly = weekly_courses(gid, now)
    collections = open_collections(gid)
    faqs = relevant_faqs(gid, asked) if asked else faq_pairs(gid)
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
    if asked:
        lines.append(f"3. 常见问题（FAQ，与本问最相关的 {len(faqs)} 条，可直接引用编号）：")
    else:
        lines.append("3. 常见问题（FAQ）：")
    lines.extend([_faq_line(item) for item in faqs] or ["- 暂无已录入 FAQ"])
    lines.append("4. 每周常规训练/任务（每周重复，无单次日期）：")
    lines.extend([_repeat_course_line(item) for item in weekly if item.get("recurring", True)]
                 or ["- 暂无周常课程"])
    return _truncate(
        "\n".join(lines),
        int(getattr(config, "QA_CONTEXT_MAX_CHARS", 3000)),
    )


def compact_business_facts(group_id: str, text: str, now: datetime | None = None) -> str:
    """只取本问相关的本群事实，最多三行、160 估算 Token；纯闲聊为空。"""
    from cdutetc.persona_chat import estimate_tokens

    gid, asked = (group_id or "").strip(), (text or "").strip()
    if gid not in config.ALLOWED_GROUPS or not asked or content_filter.is_social_chat(asked):
        return ""
    current = _now(now)
    threshold = max(0.34, config.QA_KB_MATCH_MIN_SCORE)
    candidates: list[tuple[float, str]] = []

    def short(value, limit=18):
        return _clip(" ".join(str(value or "").split()), limit)

    def add(score, line):
        if score >= threshold:
            candidates.append((score, line))

    querying = content_filter.is_information_request(asked) or bool(re.search(r"地点|教室|时间|安排", asked))
    course_query = querying and bool(re.search(r"课程|集训|上课|课表|训练|补题|周报|讲座", asked))
    sessions = weekly_courses(gid, current) if re.search(r"本周|这周", asked) else week_sessions(gid, current)
    requested_date = None
    for pattern, offset in ((r"后天", 2), (r"明天|明晚", 1), (r"今天|今晚", 0)):
        if re.search(pattern, asked):
            requested_date = (current + timedelta(days=offset)).date().isoformat()
            break
    weekday_match = re.search(r"(?:周|星期|礼拜)([一二三四五六日天])", asked)
    requested_weekday = _training_weekday(weekday_match.group(0)) if weekday_match else None
    for item in sessions:
        if requested_date and item["date_text"] != requested_date:
            continue
        if requested_weekday is not None and _training_weekday(item["weekday_label"]) != requested_weekday:
            continue
        score = content_filter.record_match_score(asked, item["title"])
        if course_query:
            score = max(score, threshold)
        add(score, f"课程 {short(item['title'])}｜{item['date_text']} {item['weekday_label']} "
                   f"{_span_text(item)}｜{short(item['location']) or '地点待定'}")
    try:
        contests = db.list_question_contests(gid, limit=20)
    except Exception:
        logger.warning("聊天事实读取比赛失败 [group=%s]", gid, exc_info=True)
        contests = []
    contest_query = querying and bool(re.search(r"比赛|竞赛|校赛|周赛|选拔赛", asked))
    for item in contests:
        name = item.get("name") or "比赛"
        score = content_filter.record_match_score(asked, name)
        moment = db._delivery_parse_time(item.get("contest_time") or "")
        if requested_date and moment is not None and moment < current:
            continue
        if moment is not None and requested_date and moment.date().isoformat() != requested_date:
            continue
        if moment is not None and requested_weekday is not None and moment.weekday() != requested_weekday:
            continue
        if contest_query and moment and current <= moment <= current + timedelta(days=7):
            score = max(score, threshold)
        time_text = item.get("contest_time") if moment else "时间待定"
        line = f"比赛 {short(name)}｜{time_text}｜{short(item.get('location')) or '地点待定'}"
        if re.search(r"报名|截止", asked):
            deadline = db._delivery_parse_time(item.get("reg_deadline") or "")
            line += f"｜报名截止 {item.get('reg_deadline') if deadline else '待定'}"
        add(score, line)
    collection_query = querying and bool(re.search(r"征集|报名|登记", asked))
    for item in open_collections(gid):
        deadline = db._delivery_parse_time(item["deadline"])
        if deadline and deadline <= current:
            continue
        score = content_filter.record_match_score(asked, item["title"])
        if collection_query:
            score = max(score, threshold)
        line = f"征集 #{item['id']} {short(item['title'])}｜截止 {item['deadline'] if deadline else '待定'}"
        if item["remaining"] is not None:
            line += f"｜剩 {item['remaining']} 名额"
        add(score, line)
    for item in relevant_faqs(gid, asked):
        add(item["score"], f"FAQ #{item['id']} {short(item['question'], 14)}：{short(item['answer'], 32)}")

    kept: list[str] = []
    for _score, line in sorted(candidates, key=lambda item: -item[0]):
        if line in kept:
            continue
        # 整行保留，避免把日期/地点裁成另一条事实。
        if estimate_tokens("\n".join([*kept, line])) <= COMPACT_FACT_TOKEN_LIMIT:
            kept.append(line)
        if len(kept) >= COMPACT_FACT_MAX_ITEMS:
            break
    return "\n".join(kept)


__all__ = [
    "CONTEXT_TITLE",
    "COMMAND_GUIDE",
    "build_group_qa_context",
    "week_sessions",
    "weekly_courses",
    "open_collections",
    "faq_pairs",
    "relevant_faqs",
    "compact_business_facts",
]

# 保留 daily_digest 引用，表明课程解析逻辑与其同源（避免重复实现）。
_ = daily_digest
