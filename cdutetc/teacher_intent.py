"""老师群内自然语言通知 → 自动识别意图并落库（不占主动推送配额）。

设计要点（三层，越靠前越省）：

1. **身份门禁 + 零 Token 预过滤**：只有该群登记的负责老师或老师白名单成员，
   且消息命中「时间锚点」或「事件锚点」时才可能调用模型。今天这类判断全部是
   本地正则，闲聊、寒暄、短句在第一步就被丢掉，不会产生任何 API 费用。
2. **模型结构化分类**：传入北京时间 + 本群课程 + 未来 7 天比赛的精简索引，
   要求输出严格 JSON（intent / confidence / 槽位），模型只做归类与抽槽，
   不做时间推算——时间一律由本地正则归一化，避免模型编造日期。
3. **分级落库**：``high`` 直接写课程/比赛并让提醒扫描自然接手；``medium``
   进 ``teacher_intent_pending`` 待补全队列并私聊老师；``CHAT_IGNORE`` 静默。

刻意不做的事：
- 不回写任何「猜出来的时间」：拿不到明确日期就进 pending；
- 竞态/异常一律「宁可不动作」：模型超时、JSON 非法、库写失败都只记日志，
  绝不发群消息，避免机器人抢话。
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from cdutetc import config, fetcher, llm
from cdutetc import database as db
from cdutetc.utils import can_manage_group

logger = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Shanghai")

# 意图分类（与需求逐字对应，禁止随意增删：落库分支按这些常量分派）
INTENT_RECURRING_COURSE = "RECURRING_COURSE"
INTENT_ONE_OFF_SESSION = "ONE_OFF_SESSION"
INTENT_CONTEST = "CONTEST"
INTENT_UPDATE_CANCEL = "UPDATE_CANCEL"
INTENT_POLICY_FAQ = "POLICY_FAQ"
INTENT_CHAT_IGNORE = "CHAT_IGNORE"

INTENTS = (
    INTENT_RECURRING_COURSE,
    INTENT_ONE_OFF_SESSION,
    INTENT_CONTEST,
    INTENT_UPDATE_CANCEL,
    INTENT_POLICY_FAQ,
    INTENT_CHAT_IGNORE,
)

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"

# 预过滤阈值：剔除 @ 与表情后的纯文本长度低于此值直接忽略。
MIN_TEXT_LENGTH = 5
# 模型调用超时（秒）：与既有意图识别保持一致，宁可放弃也不拖住消息回调。
LLM_TIMEOUT = 8.0
# 通知里带链接时抓取正文的超时（秒），需求固定 6 秒。
FETCH_TIMEOUT = 6.0
# 上下文规模上限：课程/比赛各只喂最近几条，控制 token。
MAX_COURSES_IN_CONTEXT = 10
MAX_CONTESTS_IN_CONTEXT = 8
MAX_RAW_TEXT_CHARS = 400

# 锚点一：时间表达。命中任意一项才有必要进入模型。
TIME_ANCHOR = re.compile(
    r"今天|明天|后天|今晚|周[一二三四五六日天]|下周|\d+月|\d+日|\d+号|\d{1,2}[:：点]|截止|报名"
)
# 锚点二：事件/动作表达。
EVENT_ANCHOR = re.compile(
    r"课|周报|集训|训练|机房|实验室|比赛|周赛|选拔|模拟|补题|讲座|调课|改到|取消|推迟|地点|链接|http",
    re.IGNORECASE,
)
# 锚点三：队规/制度/流程类表达。这类消息常常既无日期也无课程关键词
# （「请假要提前一天报备」「报销单据交给组长」），漏了它就会在预过滤阶段被静默丢弃。
POLICY_ANCHOR = re.compile(
    r"制度|规定|规则|规范|纪律|要求|考勤|签到|打卡|请假|报备|报销|发票|物资|经费|"
    r"值班|值日|机位|设备|不得|禁止|严禁|必须|统一|流程|申请|须知|注意事项|以后|一律",
)

# @ 段与用户粘贴的表情标记在计数前剔除。
_MENTION_RE = re.compile(r"<@!?[^>\s]*>|\[CQ:[^\]]*\]|@\S+")
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2190-\u21FF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]"
)
_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 从被抓取的正文里补地点（"地点：实训楼 B302" 这类），模型没给地点时才用。
_LOCATION_RE = re.compile(r"(?:地点|地址|教室|机房)[：: ]\s*([^\s，,。；;、|]{1,20})")


def _now(now: datetime | None = None) -> datetime:
    """统一取北京时间，测试可注入固定时刻。"""
    if now is None:
        return datetime.now(TZ).replace(tzinfo=None)
    return now.astimezone(TZ).replace(tzinfo=None) if now.tzinfo else now


def normalize_text(text: str) -> str:
    """剔除 @ 段与表情，压平空白；返回用于长度判断与锚点匹配的纯文本。"""
    cleaned = _MENTION_RE.sub(" ", text or "")
    cleaned = _EMOJI_RE.sub(" ", cleaned)
    return " ".join(cleaned.split())


def is_teacher_speaker(group_id: str, sender_id: str) -> bool:
    """身份门禁：老师白名单 / 超管 / 该群已登记的负责老师，三者之一才算老师。"""
    speaker = str(sender_id or "").strip()
    if not speaker:
        return False
    if can_manage_group(speaker):
        return True
    if not str(group_id or "").strip():
        return False
    try:
        return speaker in set(db.get_group_teachers(group_id))
    except Exception:
        # 库异常时按「不是老师」处理：宁可漏识别，也不能让普通成员的通知落库。
        logger.warning("读取群负责老师失败，按非老师处理: %s", group_id)
        return False


def should_call_llm(text: str) -> bool:
    """第一层：0 Token 预过滤。长度不足或无任何锚点 → 不调模型。"""
    plain = normalize_text(text)
    if len(plain) < MIN_TEXT_LENGTH:
        return False
    return bool(
        TIME_ANCHOR.search(plain)
        or EVENT_ANCHOR.search(plain)
        or POLICY_ANCHOR.search(plain)
    )


def _weekday_of(moment: datetime) -> str:
    return _WEEKDAYS[moment.weekday()]


def _parse_date(value: str, now: datetime) -> str:
    """把模型给的日期收敛到 YYYY-MM-DD；只接受合法日期，其余返回空串。"""
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("/", "-").replace(".", "-")
    if not _DATE_RE.match(text):
        return ""
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return ""
    return text


def _parse_clock(value: str) -> str:
    """把「19:00 / 19点 / 19：00」统一成 HH:MM；非法返回空串。"""
    text = str(value or "").strip().replace("：", ":")
    match = re.match(r"^(\d{1,2})(?::(\d{1,2}))?\s*[点时]?$", text)
    if not match:
        return ""
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return ""
    return f"{hour:02d}:{minute:02d}"


def _parse_weekday(value: str) -> str:
    """把「周三 / 星期三 / 每周三」统一成「周三」。"""
    text = str(value or "").strip()
    match = re.search(r"周([一二三四五六日天])", text)
    if match:
        return f"周{'日' if match.group(1) == '天' else match.group(1)}"
    match = re.search(r"(?:星期|礼拜)([一二三四五六日天])", text)
    if match:
        return f"周{'日' if match.group(1) == '天' else match.group(1)}"
    return ""


def _sanitize_url(value: str) -> str:
    """只放行 http(s)、带主机名且不带账号密码的链接（与库侧校验口径一致）。"""
    text = str(value or "").strip()
    if not text.lower().startswith(("http://", "https://")):
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    if not parts.netloc or parts.hostname is None:
        return ""
    if parts.username is not None or parts.password is not None:
        return ""
    return text


def _contest_index(group_id: str, now: datetime) -> list[dict]:
    """未来 7 天、已确认且有开赛时间的比赛精简索引（只留 ID/名称/时间）。"""
    horizon = now + timedelta(days=7)
    items: list[dict] = []
    try:
        contests = db.list_group_competitions(group_id, status="confirmed")
    except Exception:
        logger.warning("读取比赛列表失败，上下文不含比赛索引")
        return items
    for item in contests:
        raw_time = str(item.get("official_time") or "").strip()
        if not raw_time:
            continue
        try:
            moment = datetime.strptime(raw_time, "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if not (now <= moment <= horizon):
            continue
        items.append({"id": item.get("id"), "name": item.get("name") or "",
                      "time": moment.strftime("%m-%d %H:%M")})
        if len(items) >= MAX_CONTESTS_IN_CONTEXT:
            break
    return items


def _course_index(group_id: str) -> list[dict]:
    """本群当前生效课程的精简索引。"""
    items: list[dict] = []
    try:
        sessions = db.list_training_sessions(group_id)
    except Exception:
        logger.warning("读取课程列表失败，上下文不含课程索引: %s", group_id)
        return items
    for session in sessions:
        items.append({
            "id": session.get("id"),
            "title": session.get("title") or "",
            "weekday": session.get("weekday") or "",
            "start_time": session.get("start_time") or "",
            "location": session.get("location") or "",
        })
        if len(items) >= MAX_COURSES_IN_CONTEXT:
            break
    return items


def build_context(group_id: str, now: datetime) -> tuple[list[dict], list[dict]]:
    return _course_index(group_id), _contest_index(group_id, now)


def _system_prompt(now: datetime) -> str:
    # 提示词里显式带「JSON」，DeepSeek 的 json_object 模式要求出现该字样。
    return (
        "你是 ACM 集训队群消息的意图解析器，只输出一个 JSON 对象，不要解释、不要代码块。\n"
        f"当前北京时间：{now:%Y-%m-%d %H:%M} {_weekday_of(now)}。\n"
        "输出字段：\n"
        '{"intent":"RECURRING_COURSE|ONE_OFF_SESSION|CONTEST|UPDATE_CANCEL|POLICY_FAQ|CHAT_IGNORE",'
        '"confidence":"high|medium","title":"","date":"","weekday":"","start_time":"",'
        '"end_time":"","location":"","url":"","target_id":0,"cancel":false,'
        '"missing":["造成不完整所缺的信息英文键"]}\n'
        "判定规则：\n"
        "- RECURRING_COURSE：周期课程，如「每周三晚7点在B302讲图论」，给 weekday。\n"
        "- ONE_OFF_SESSION：单次集训/周报/补题课，给 date 与 start_time。\n"
        "- CONTEST：比赛通知（含周赛/选拔/模拟赛），给 title、date、start_time，有链接填 url。\n"
        "- UPDATE_CANCEL：修改或取消已有课程/比赛，target_id 填索引里的编号，"
        "cancel=true 表示取消。\n"
        "- POLICY_FAQ：老师发布需要长期遵守的队规/制度/流程要求，没有具体某一天的时间锚点，"
        "例如「以后请假必须提前一天在群里报备」「报销单据周五前交给组长」「训练期间禁止带外队人员进机房」。"
        "title 填规则主题（如「请假流程」），这类消息 confidence 一律 high。\n"
        "- CHAT_IGNORE：闲聊、鼓励、感想、批评训话（即使含时间词或「以后」），"
        "例如「昨晚CF打得不错，下周继续保持」「大家最近状态不错，继续努力」。\n"
        "- 提问、求推荐、寒暄、@他人，一律 CHAT_IGNORE。\n"
        "- 既是规则又含一次性日程的消息（如「这周三晚七点讲图论，以后迟到不许进教室」）"
        "按日程处理，归 RECURRING_COURSE / ONE_OFF_SESSION，不要归 POLICY_FAQ。\n"
        "- confidence：时间与事项都能直接执行才 high；出现「找个晚上」「下周再说」「待定」"
        "这类模糊表达必须 medium，并在 missing 里写明缺什么。\n"
        "- 日期只输出 YYYY-MM-DD，时间只输出 HH:MM，不要自己推算相对日期。"
    )


def _user_prompt(text: str, courses: list[dict], contests: list[dict]) -> str:
    payload = {
        "message": normalize_text(text)[:MAX_RAW_TEXT_CHARS],
        "courses": courses,
        "contests": contests,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def parse_result(content: str) -> dict | None:
    """解析并校验模型输出；任何不合法都返回 None（调用方静默放弃）。"""
    data = llm._load_json_object(content)
    if not isinstance(data, dict):
        return None
    intent = str(data.get("intent") or "").strip().upper()
    if intent not in INTENTS:
        return None
    confidence = str(data.get("confidence") or "").strip().lower()
    if confidence not in (CONFIDENCE_HIGH, CONFIDENCE_MEDIUM):
        # 模型没给可信置信度时按 medium 处理：不确定就不要自动排期。
        confidence = CONFIDENCE_MEDIUM
    raw_missing = data.get("missing")
    missing = [str(item).strip() for item in raw_missing if str(item).strip()] \
        if isinstance(raw_missing, list) else []
    try:
        target_id = int(data.get("target_id") or 0)
    except (TypeError, ValueError):
        target_id = 0
    return {
        "intent": intent,
        "confidence": confidence,
        "title": str(data.get("title") or "").strip()[:60],
        "date": str(data.get("date") or "").strip(),
        "weekday": str(data.get("weekday") or "").strip(),
        "start_time": str(data.get("start_time") or "").strip(),
        "end_time": str(data.get("end_time") or "").strip(),
        "location": str(data.get("location") or "").strip()[:40],
        "url": str(data.get("url") or "").strip(),
        "target_id": target_id,
        "cancel": bool(data.get("cancel")),
        "missing": missing,
    }


async def _fetch_url_summary(url: str) -> str:
    """比赛通知带链接时补抓正文（6 秒超时）；失败一律返回空串，不影响主流程。"""
    if not url:
        return ""
    try:
        body = await asyncio.wait_for(fetcher.fetch_text(url), timeout=FETCH_TIMEOUT)
    except Exception as error:
        logger.info("老师意图链接抓取失败: %s", type(error).__name__)
        return ""
    return " ".join(str(body or "").split())[:600]


def _location_from_summary(summary: str) -> str:
    """正文没结构化字段，只按「地点：xxx」这类显式标签兜底，抽不到就留空。"""
    match = _LOCATION_RE.search(summary or "")
    return match.group(1).strip() if match else ""


async def classify(text: str, *, group_id: str, now: datetime | None = None) -> dict | None:
    """调用模型做结构化分类；超时/异常返回 None。"""
    moment = _now(now)
    courses, contests = build_context(group_id, moment)
    try:
        raw = await asyncio.wait_for(
            llm._chat_once(
                _system_prompt(moment),
                _user_prompt(text, courses, contests),
                json_mode=True,
                max_tokens=300,
            ),
            timeout=LLM_TIMEOUT,
        )
    except Exception as error:
        logger.warning("老师意图分类调用失败: %s", type(error).__name__)
        return None
    return parse_result(raw)


# ---------------------------------------------------------------------
# 槽位补全：模型只管归类，时间与标题在这里落成可直接入库的字段
# ---------------------------------------------------------------------


def _slot_date(slots: dict, now: datetime) -> datetime | None:
    """由槽位还原出具体日期（date 优先，其次 weekday 推最近一次）。"""
    date = _parse_date(slots.get("date"), now)
    if date:
        try:
            return datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            return None
    weekday = _parse_weekday(slots.get("weekday"))
    if weekday:
        target = _WEEKDAYS.index(weekday)
        delta = (target - now.weekday()) % 7
        return (now + timedelta(days=delta)).replace(hour=0, minute=0, second=0, microsecond=0)
    return None


def _is_high_ready(intent: str, slots: dict, now: datetime) -> bool:
    """high 是否真的可以直接落库：缺关键槽位就降级为待补全。"""
    if intent in (INTENT_RECURRING_COURSE,):
        return bool(_parse_weekday(slots.get("weekday")) or _parse_date(slots.get("date"), now)) \
            and bool(_parse_clock(slots.get("start_time")))
    if intent in (INTENT_ONE_OFF_SESSION, INTENT_CONTEST):
        return bool(_parse_date(slots.get("date"), now)) and bool(_parse_clock(slots.get("start_time")))
    if intent == INTENT_UPDATE_CANCEL:
        return bool(slots.get("target_id"))
    return False


def _missing_slots(intent: str, slots: dict, now: datetime) -> list[str]:
    gaps: list[str] = []
    if intent == INTENT_RECURRING_COURSE:
        if not (_parse_weekday(slots.get("weekday")) or _parse_date(slots.get("date"), now)):
            gaps.append("weekday")
        if not _parse_clock(slots.get("start_time")):
            gaps.append("start_time")
    elif intent in (INTENT_ONE_OFF_SESSION, INTENT_CONTEST):
        if not _parse_date(slots.get("date"), now):
            gaps.append("date")
        if not _parse_clock(slots.get("start_time")):
            gaps.append("start_time")
    elif intent == INTENT_UPDATE_CANCEL and not slots.get("target_id"):
        gaps.append("target_id")
    return gaps


def _course_info(intent: str, slots: dict, now: datetime) -> dict:
    """把槽位组装成 training_sessions 的入库字段。"""
    moment = _slot_date(slots, now)
    weekday = _parse_weekday(slots.get("weekday"))
    start = _parse_clock(slots.get("start_time"))
    end = _parse_clock(slots.get("end_time"))
    info: dict = {"start_time": start, "end_time": end}
    if intent == INTENT_RECURRING_COURSE:
        # 周期课不写死生效期：写了「仅那一天」下个月就自动失效了。
        info["weekday"] = weekday or (_weekday_of(moment) if moment else "")
    else:
        # 单次集训只在当天生效，靠 effective_start/end 把提醒窗口夹住。
        info["weekday"] = _weekday_of(moment) if moment else ""
        if moment is not None:
            info["effective_start"] = f"{moment:%Y-%m-%d} 00:00"
            info["effective_end"] = f"{moment:%Y-%m-%d} 23:59"
    info["title"] = slots.get("title") or (
        "集训" if intent == INTENT_ONE_OFF_SESSION else "课程")
    info["location"] = slots.get("location") or ""
    info["source"] = "teacher_intent"
    info["status"] = "confirmed"
    return info


def _course_receipt(course: dict, *, prefix: str = "📌 已自动登记课程") -> str:
    """极简确认卡片：只保留标题/时间/地点三要素，一条消息看完。"""
    head = f"{prefix}【{course.get('title') or '课程'}】"
    if course.get("effective_start"):
        day = str(course["effective_start"])[:10]
        span = f"{day} {course.get('start_time') or ''}".strip()
    else:
        span = f"{course.get('weekday') or '时间待定'} {course.get('start_time') or ''}".strip()
    if course.get("end_time"):
        span = f"{span}-{course['end_time']}"
    line = f"{head}｜{span}"
    location = str(course.get("location") or "").strip()
    return f"{line}｜{location}" if location else line


def _contest_receipt(contest: dict, *, prefix: str = "📌 已自动登记比赛") -> str:
    head = f"{prefix}【{contest.get('name') or '比赛'}】"
    when = str(contest.get("official_time") or "").strip()
    return f"{head}｜{when}" if when else head


def _save_course(group_id: str, intent: str, slots: dict, now: datetime,
                 operator: str) -> dict | None:
    """写入课程并让提醒扫描自然接手（课程提醒是扫描式，落库即排队）。"""
    info = _course_info(intent, slots, now)
    if not info.get("start_time"):
        return None
    info["maintainer"] = operator
    course_id = db.create_training_session(group_id, info, operator=operator)
    return db.get_group_course(group_id, course_id)


def _save_contest(group_id: str, slots: dict, now: datetime, operator: str) -> dict | None:
    """写入比赛并绑定本群提醒范围；开赛前 15 分钟节点由既有同步逻辑生成。"""
    moment = _slot_date(slots, now)
    start = _parse_clock(slots.get("start_time"))
    if moment is None or not start:
        return None
    official_time = f"{moment:%Y-%m-%d} {start}"
    try:
        cid = db.create_competition(
            {
                "name": slots.get("title") or "比赛",
                "official_time": official_time,
                "official_site": _sanitize_url(slots.get("url")),
            },
            status="confirmed",
            operator=operator,
        )
    except Exception:
        logger.exception("老师意图写比赛失败")
        return None
    if not cid:
        return None
    try:
        db.set_competition_notify_groups(cid, {group_id}, operator)
    except Exception:
        logger.warning("比赛提醒范围绑定失败: %s", cid)
    return db.get_competition(cid)


def _apply_update(group_id: str, slots: dict, now: datetime, operator: str) -> tuple[str, dict] | None:
    """改期/改地点/取消：先定对象类型，再改字段，最后作废旧提醒节点。"""
    target_id = int(slots.get("target_id") or 0)
    if target_id <= 0:
        return None
    cancel = bool(slots.get("cancel"))
    course = db.get_group_course(group_id, target_id)
    if course is not None:
        if cancel:
            if not db.cancel_training_session(group_id, target_id, operator):
                return None
            db.drop_pending_deliveries("training_session", target_id, group_id=group_id)
            return "course_cancelled", course
        updates: dict = {}
        weekday = _parse_weekday(slots.get("weekday"))
        start = _parse_clock(slots.get("start_time"))
        end = _parse_clock(slots.get("end_time"))
        if weekday:
            updates["weekday"] = weekday
        if start:
            updates["start_time"] = start
        if end:
            updates["end_time"] = end
        if slots.get("location"):
            updates["location"] = slots["location"]
        if slots.get("title"):
            updates["title"] = slots["title"]
        if not updates:
            return None
        if not db.update_group_course(group_id, target_id, operator, **updates):
            return None
        # 旧的 24h/1h 节点已经对应不上新时间，清掉后被扫描式计划重建。
        db.drop_pending_deliveries("training_session", target_id, group_id=group_id)
        fresh = db.get_group_course(group_id, target_id) or course
        return "course_updated", fresh
    contest = db.get_competition(target_id)
    if (contest is None or contest.get("status") != "confirmed"
            or group_id not in db.get_competition_target_groups(target_id)):
        return None
    if cancel:
        from cdutetc import group_cleanup

        try:
            if not group_cleanup.archive_competition(group_id, target_id, operator=operator,
                                                     reason="cancelled"):
                return None
        except ValueError:
            # 发送中的对象留待补全，不取消在途租约。
            return None
        return "contest_cancelled", contest
    updates = {}
    moment = _slot_date(slots, now)
    start = _parse_clock(slots.get("start_time"))
    if moment is not None and start:
        updates["official_time"] = f"{moment:%Y-%m-%d} {start}"
    if slots.get("location"):
        updates["summary"] = slots["location"]
    if slots.get("url"):
        updates["official_site"] = _sanitize_url(slots["url"])
    if not updates:
        return None
    if not db.update_competition(target_id, updates, operator, "老师群内通知改期"):
        return None
    fresh = db.get_competition(target_id) or contest
    return "contest_updated", fresh


def _private_targets(group_id: str, sender_id: str) -> list[str]:
    """模糊通知要私下催的人：先老师本人，再该群负责老师与超管。"""
    targets: list[str] = []
    for uid in [sender_id, *sorted(config.SUPER_ADMIN)]:
        if uid and uid not in targets:
            targets.append(uid)
    try:
        for uid in sorted(db.get_group_teachers(group_id)):
            if uid and uid not in targets:
                targets.append(uid)
    except Exception:
        logger.warning("读取群负责老师失败，待补全通知只发本人与超管")
    return targets


def _pending_private_text(item: dict) -> str:
    gap_labels = {"date": "日期", "weekday": "周几", "start_time": "开始时间",
                  "end_time": "结束时间", "location": "地点", "target_id": "要改的对象"}
    missing = "、".join(gap_labels.get(item, item) for item in item["missing"]) or "关键信息"
    return (
        f"收到一条待补全通知（{ctx_label(item['intent'])}）。\n"
        f"缺：{missing}\n"
        f"原文：{item['raw_text'][:120]}\n"
        f"请补全后直接发命令登记，或在群里补一句完整时间。"
    )


def ctx_label(intent: str) -> str:
    return {
        INTENT_RECURRING_COURSE: "周期课程",
        INTENT_ONE_OFF_SESSION: "单次集训",
        INTENT_CONTEST: "比赛",
        INTENT_UPDATE_CANCEL: "改期/取消",
        INTENT_POLICY_FAQ: "队规/制度",
    }.get(intent, "通知")


def _result(intent: str, action: str, *, reply: str = "",
            notify: list[tuple[str, str]] | None = None,
            object_id: int | None = None) -> dict:
    return {"intent": intent, "action": action, "reply": reply or "",
            "notify_private": notify or [], "object_id": object_id}


async def handle_message(text: str, *, group_id: str, sender_id: str,
                         msg_id: str = "", now: datetime | None = None) -> dict | None:
    """老师非命令群消息的唯一入口。

    Returns:
        None                      → 不是老师 / 与本管线无关，调用方按原逻辑处理；
        {"action": "ignored"}     → 老师说话但判定为闲聊或无法解析，调用方静默；
        {"action": "saved"}       → 已落库，``reply`` 为该消息的被动回执；
        {"action": "pending"}     → 已进待补全队列，``notify_private`` 为私聊催办。
    """
    if not is_teacher_speaker(group_id, sender_id):
        return None
    moment = _now(now)
    if not should_call_llm(text):
        # 第一层拦下的都是闲聊：不花 token，也不在群里留下任何痕迹。
        return _result(INTENT_CHAT_IGNORE, "ignored")
    slots = await classify(text, group_id=group_id, now=moment)
    if slots is None:
        return _result(INTENT_CHAT_IGNORE, "ignored")
    intent = slots["intent"]
    if intent == INTENT_CHAT_IGNORE:
        return _result(intent, "ignored")
    # 队规/制度类不进日程落库链：先提炼成问答草稿，等老师私聊点头才生效。
    if intent == INTENT_POLICY_FAQ:
        return await _apply_policy_faq(text, slots, group_id, sender_id)
    # 比赛链接补充：正文里的「地点：xxx」能补上模型漏掉的场地，
    # 抓取失败只是少一点补充信息，不影响落库。
    if intent == INTENT_CONTEST:
        url = _sanitize_url(slots.get("url"))
        if url:
            slots["url"] = url
            summary = await _fetch_url_summary(url)
            if summary and not slots.get("location"):
                slots["location"] = _location_from_summary(summary)
    confidence = slots["confidence"]
    if confidence == CONFIDENCE_HIGH and _is_high_ready(intent, slots, moment):
        return await _apply_high(text, slots, group_id, sender_id, moment, msg_id)
    return _apply_medium(text, slots, group_id, sender_id, moment, msg_id)


async def _apply_high(text: str, slots: dict, group_id: str, sender_id: str,
                      now: datetime, msg_id: str) -> dict:
    """高置信：直接落库 + 用该消息 msg_id 被动回执（不耗主动配额）。"""
    intent = slots["intent"]
    if intent == INTENT_UPDATE_CANCEL:
        applied = _apply_update(group_id, slots, now, sender_id)
        if applied is None:
            # 目标找不到就转成待补全，绝不瞎改一条不相干的课程/比赛。
            return _apply_medium(text, slots, group_id, sender_id, now, msg_id)
        action, obj = applied
        if action == "course_cancelled":
            reply = _course_receipt(obj, prefix="🗑 已取消课程")
        elif action == "course_updated":
            reply = _course_receipt(obj, prefix="🔁 已更新课程")
        elif action == "contest_cancelled":
            reply = _contest_receipt(obj, prefix="🗑 已取消比赛推送")
        else:
            reply = _contest_receipt(obj, prefix="🔁 已更新比赛")
        return _result(intent, "saved", reply=reply,
                       object_id=int(obj.get("id") or 0) or None)
    if intent == INTENT_CONTEST:
        contest = _save_contest(group_id, slots, now, sender_id)
        if contest is None:
            return _apply_medium(text, slots, group_id, sender_id, now, msg_id)
        return _result(intent, "saved", reply=_contest_receipt(contest),
                       object_id=contest.get("id"))
    course = _save_course(group_id, intent, slots, now, sender_id)
    if course is None:
        return _apply_medium(text, slots, group_id, sender_id, now, msg_id)
    prefix = "📌 已自动登记集训" if intent == INTENT_ONE_OFF_SESSION else "📌 已自动登记课程"
    return _result(intent, "saved", reply=_course_receipt(course, prefix=prefix),
                   object_id=course.get("id"))


def _policy_private_text(topic: str, created: list[dict], duplicates: list[dict]) -> str:
    """私聊确认卡：只列草稿编号 + 问答，一句话说清怎么回。"""
    lines = [f"📋 队规问答草稿·{topic}（共 {len(created)} 条，等你确认）"]
    for item in created:
        lines.append(f'#{item["faq_id"]}')
        lines.append(f'Q: {item["question"]}')
        lines.append(f'A: {item["answer"]}')
        lines.append("")
    if duplicates:
        lines.append("以下与已录入问答重复，未重复建档：")
        lines.extend(f'- {item["question"]}' for item in duplicates[:5])
        lines.append("")
    lines.append(
        "回复「确认 编号」让它生效，「FAQ驳回 编号」丢弃；"
        "多条可写「确认 12 13」，「确认 全部」/「FAQ驳回 全部」一次处理。\n"
        "不想私聊处理，也可以在群里用 /FAQ待确认、/FAQ确认、/FAQ驳回。"
    )
    return "\n".join(lines).strip()


async def _apply_policy_faq(text: str, slots: dict, group_id: str, sender_id: str) -> dict:
    """队规/制度通知：提炼问答草稿写 pending，私聊老师确认后才生效。

    刻意不做的事：绝不直接写 confirmed——老师一句「以后别迟到」不该自动变成队规，
    必须本人点头；提炼不到可复用规则、或与既有问答重复时，群里保持安静或只回一句
    「未重复建档」，绝不刷屏。
    """
    if not getattr(config, "TEACHER_POLICY_FAQ_ENABLED", True):
        return _result(INTENT_POLICY_FAQ, "ignored")
    drafts = await llm.extract_faqs(normalize_text(text))
    if not drafts:
        return _result(INTENT_POLICY_FAQ, "ignored")
    topic = str(slots.get("title") or "").strip() or "队规"
    created: list[dict] = []
    duplicates: list[dict] = []
    for draft in drafts:
        try:
            outcome = db.add_faq_pending(
                group_id, draft["question"], draft["answer"],
                source=db.teacher_source(sender_id), operator=sender_id,
            )
        except Exception:
            logger.exception("队规问答草稿入库失败 [group=%s]", group_id)
            continue
        (created if outcome.get("created") else duplicates).append(outcome)
    if not created and not duplicates:
        return _result(INTENT_POLICY_FAQ, "ignored")
    if created:
        reply = f"📝 收到「{topic}」相关队规，已提炼 {len(created)} 条问答草稿，私聊确认后生效。"
    else:
        reply = f"📝 这条队规和已录入问答重复（{len(duplicates)} 条），未重复建档。"
    notify = [(sender_id, _policy_private_text(topic, created, duplicates))] if created else []
    return _result(
        INTENT_POLICY_FAQ, "pending", reply=reply, notify=notify,
        object_id=(created[0]["faq_id"] if created else None),
    )


def _apply_medium(text: str, slots: dict, group_id: str, sender_id: str,
                  now: datetime, msg_id: str) -> dict:
    """时间/对象模糊：进待补全队列，私聊催老师补全，群里只留一句极简回执。"""
    intent = slots["intent"]
    missing = slots.get("missing") or _missing_slots(intent, slots, now)
    try:
        pid = db.add_teacher_intent_pending(
            group_id, sender_id=sender_id, msg_id=msg_id, raw_text=normalize_text(text),
            intent=intent, slots=slots, missing=missing,
        )
    except Exception:
        logger.exception("待补全通知入库失败")
        return _result(intent, "ignored")
    record = db.get_teacher_intent_pending(pid) if pid else None
    gap_labels = {"date": "日期", "weekday": "周几", "start_time": "开始时间",
                  "end_time": "结束时间", "location": "地点", "target_id": "要改的课程/比赛"}
    text_gaps = "、".join(gap_labels.get(item, item) for item in missing)
    if text_gaps:
        reply = f"📝 已记下{ctx_label(intent)}通知，还缺{text_gaps}，稍后私聊确认。"
    else:
        reply = f"📝 已记下{ctx_label(intent)}通知，时间还不够明确，稍后私聊确认。"
    notify: list[tuple[str, str]] = []
    if record is not None:
        private_text = _pending_private_text(record)
        notify = [(uid, private_text) for uid in _private_targets(group_id, sender_id)]
    return _result(intent, "pending", reply=reply, notify=notify,
                   object_id=(record or {}).get("id"))
