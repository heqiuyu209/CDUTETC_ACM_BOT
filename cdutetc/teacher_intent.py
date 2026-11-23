"""老师群内自然语言通知 → 自动识别意图并落库（不占主动推送配额）。

设计要点（三层，越靠前越省）：

1. **身份门禁 + 零 Token 预过滤**：只有该群登记的负责老师或老师白名单成员，
   且消息命中「明确钟点」或「事件/规则锚点」时才可能调用模型。这些判断全部是
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
import unicodedata
from contextlib import closing
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from cdutetc import config, content_filter, fetcher, llm, publish_text
from cdutetc import database as db
from cdutetc.utils import can_manage_group, split_normalized_time

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

# 事件/动作锚点；单独的日期词不触发，避免“今天好开心”支付分类费用。
EVENT_ANCHOR = re.compile(
    r"课|周报|集训|训练|机房|实验室|比赛|周赛|报名|截止|选拔|模拟|补题|讲座|讲\s*(?:DP|图论|算法|数据结构)|调课|改到|取消|推迟|地点|链接|http",
    re.IGNORECASE,
)
# 队规/制度/流程锚点。这类消息常常既无日期也无课程关键词
# （「请假要提前一天报备」「报销单据交给组长」），漏了它就会在预过滤阶段被静默丢弃。
POLICY_ANCHOR = re.compile(
    r"制度|规定|规则|规范|纪律|要求|考勤|签到|打卡|请假|报备|报销|发票|物资|经费|"
    r"值班|值日|机位|设备|不得|禁止|严禁|必须|统一|流程|申请|须知|注意事项|以后|一律",
)

# @ 段与用户粘贴的表情标记在计数前剔除。
_MENTION_RE = re.compile(r"<@!?[^>\s]*>|\[CQ:[^\]]*\]|@(?:全体成员|所有人|全体|everyone|all)(?![A-Za-z])|@[^\s，,。；;：:]+(?=\s)")
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
    if group_id not in config.ALLOWED_GROUPS:
        return False
    if speaker in config.SUPER_ADMIN:
        return True
    try:
        owners = set(db.get_group_teachers(group_id))
        return can_manage_group(speaker) and (not owners or speaker in owners)
    except Exception:
        # 库异常时按「不是老师」处理：宁可漏识别，也不能让普通成员的通知落库。
        logger.warning("读取群负责老师失败，按非老师处理: %s", group_id)
        return False


def should_call_llm(text: str) -> bool:
    """第一层：0 Token 预过滤。长度不足或无任何锚点 → 不调模型。"""
    plain = normalize_text(text)
    if publish_text._QUERY.search(plain) or content_filter.is_social_chat(plain):
        return False
    if len(plain) < MIN_TEXT_LENGTH:
        return False
    # 只有“今天好开心”这类日期词不是通知；事件/规则或明确钟点才值得分类。
    return bool(EVENT_ANCHOR.search(plain) or POLICY_ANCHOR.search(plain)
                or publish_text._CLOCK.search(unicodedata.normalize("NFKC", plain)))


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
        items.append({"id": item.get("id"), "type": "competition", "name": item.get("name") or "",
                      "time": moment.strftime("%Y-%m-%d %H:%M"), "location": item.get("location") or ""})
        if len(items) >= MAX_CONTESTS_IN_CONTEXT:
            break
    return items


def _course_index(group_id: str, now: datetime | None = None) -> list[dict]:
    """本群当前生效课程的精简索引。"""
    items: list[dict] = []
    try:
        from cdutetc import qa_context
        sessions = qa_context.week_sessions(group_id, now=now)
    except Exception:
        logger.warning("读取课程列表失败，上下文不含课程索引: %s", group_id)
        return items
    for session in sessions:
        items.append({
            "id": session.get("id"),
            "type": "training_session",
            "title": session.get("title") or "",
            "date": session["start_dt"].strftime("%Y-%m-%d"),
            "weekday": session.get("weekday_label") or "",
            "start_time": session.get("start_text") or "",
            "end_time": session.get("end_text") or "",
            "location": session.get("location") or "",
        })
        if len(items) >= MAX_COURSES_IN_CONTEXT:
            break
    return items


def build_context(group_id: str, now: datetime) -> tuple[list[dict], list[dict]]:
    return _course_index(group_id, now), _contest_index(group_id, now)


def _system_prompt(now: datetime) -> str:
    # 提示词里显式带「JSON」，DeepSeek 的 json_object 模式要求出现该字样。
    return (
        "你是 ACM 集训队群消息的意图解析器，只输出一个 JSON 对象，不要解释、不要代码块。\n"
        f"当前北京时间：{now:%Y-%m-%d %H:%M} {_weekday_of(now)}。\n"
        "输出字段：\n"
        '{"intent":"RECURRING_COURSE|ONE_OFF_SESSION|CONTEST|UPDATE_CANCEL|POLICY_FAQ|CHAT_IGNORE",'
        '"confidence":"high|medium","title":"","date":"","weekday":"","start_time":"",'
        '"end_time":"","location":"","url":"","target_id":0,"target_type":"","cancel":false,'
        '"missing":["造成不完整所缺的信息英文键"]}\n'
        "判定规则：\n"
        "- RECURRING_COURSE：周期课程，如「每周三晚7点在B302讲图论」，给 weekday。\n"
        "- ONE_OFF_SESSION：单次集训/周报/补题课，给 date 与 start_time。\n"
        "- CONTEST：比赛通知（含周赛/选拔/模拟赛），给 title、date、start_time，有链接填 url。\n"
        "- UPDATE_CANCEL：修改或取消已有课程/比赛，target_id 填索引里的编号，target_type 填 training_session 或 competition，"
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
        "target_type": str(data.get("target_type") or "").strip(),
        "cancel": data.get("cancel") is True,
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


def _declared_schedule(text: str, slots: dict, now: datetime) -> dict:
    """模型只分类；日程只能来自老师原文，本地解析失败则保留为空待补全。"""
    result = dict(slots)
    intent = result["intent"]
    if intent not in {INTENT_RECURRING_COURSE, INTENT_ONE_OFF_SESSION,
                      INTENT_CONTEST, INTENT_UPDATE_CANCEL}:
        return result
    source = unicodedata.normalize("NFKC", normalize_text(text))
    source = re.sub(r"(?<![今明])晚(?=[零〇一二两三四五六七八九十\d])", "晚上", source)
    # “19-21点”只有尾端单位，先补齐起点，再用既有时间区间规则。
    source = re.sub(r"(?<![\d:])([零〇一二两三四五六七八九十\d]+)\s*([-~～—–到至])\s*"
                    r"(?=[零〇一二两三四五六七八九十\d]+[点时])", r"\1点\2", source)
    time_only_update = intent == INTENT_UPDATE_CANCEL and bool(re.search(r"时间\s*(?:改为|改到|改成|调整到)", source))
    if intent == INTENT_UPDATE_CANCEL:
        # 「周三的课改到周四」以改到后的日期为准，不取被改的旧时间。
        move = re.search(r"改到|改为|换到|调整到|推迟到", source)
        if move and not time_only_update:
            source = source[move.end():]
    rescheduling = intent == INTENT_UPDATE_CANCEL and not result.get("cancel") and bool(
        re.search(r"改到|改为|调整到|推迟|延后|提前", normalize_text(text))
        and (publish_text._CLOCK.search(source) or publish_text._RELATIVE.search(source)
             or publish_text._DATE.search(source) or publish_text._MONTH_DAY.search(source)
             or re.search(r"下周|本周|这周|周[一二三四五六日天]|半小时|小时|分钟|待定", source)))
    recurring = bool(re.search(r"每\s*(?:周|星期|礼拜)[一二三四五六日天]", source))
    if intent in {INTENT_RECURRING_COURSE, INTENT_ONE_OFF_SESSION}:
        result["intent"] = INTENT_RECURRING_COURSE if recurring else INTENT_ONE_OFF_SESSION
    for field in ("date", "weekday", "start_time", "end_time"):
        result[field] = ""
    dates = list(publish_text._DATE.finditer(source))
    month_days = [match for match in publish_text._MONTH_DAY.finditer(source)
                  if not any(day.start() <= match.start() < day.end() for day in dates)]
    relatives = list(publish_text._RELATIVE.finditer(source))
    weekdays = list(publish_text._WEEKDAY.finditer(source))
    has_date = bool(dates or month_days or relatives or weekdays)
    clocks = list(publish_text._CLOCK.finditer(source))
    clock_source = source
    start = ""
    invalid_span = False
    is_span = False
    if len(clocks) == 2 and re.fullmatch(r"\s*[-~～—–到至]\s*", source[clocks[0].end():clocks[1].start()]):
        is_span = True
        span = source[clocks[0].start():clocks[1].end()]
        if re.search(r"今晚|明晚", source[:clocks[0].start()]):
            span = "晚上" + span
        _, start, end = split_normalized_time(span)
        invalid_span = not start or not end
        result["end_time"] = end
        clock_source = source[:clocks[0].end()] + source[clocks[1].end():]
    try:
        parsed = None if invalid_span else publish_text.parse_local_time(clock_source, now=now)
    except ValueError:
        parsed = None
    # parse_local_time 为显式发布保留“今天”的默认；自动识别必须有原文日期。
    if parsed is not None:
        result["start_time"] = parsed[0][11:]
        if is_span and (result["start_time"] != start or result["end_time"] <= result["start_time"]):
            # 区间跨午夜/时段不一致时不套用另一套钟点含义，也不猜跨天结束。
            result["start_time"] = result["end_time"] = ""
    else:
        result["end_time"] = ""
    unambiguous_date = (len(dates) + len(month_days) + len(relatives) <= 1
                        and len(weekdays) <= 1)
    if has_date and unambiguous_date:
        # 没有钟点也能解析明确日期，从而只向老师索要真正缺失的开始时间。
        day_source = publish_text._CLOCK.sub("", clock_source)
        day_source = re.sub(r"明晚|明早", "明天", day_source)
        day_source = re.sub(r"今晚|今早", "今天", day_source) + " 00:00"
        try:
            day = publish_text.parse_local_time(day_source, now=now)
        except ValueError:
            day = None
        if parsed is not None or day is not None:
            result["date"] = (parsed or day)[0][:10]
            result["weekday"] = _weekday_of(datetime.strptime(result["date"], "%Y-%m-%d"))
            if weekdays and (dates or month_days or relatives) and (
                    result["weekday"] != _parse_weekday(weekdays[0][0])):
                result["date"] = result["weekday"] = ""
    if recurring and len(weekdays) == 1:
        result["weekday"] = _parse_weekday(weekdays[0][0])
        result["date"] = ""
    gaps = _missing_slots(result["intent"], result, now)
    if recurring and (dates or month_days or relatives or re.search(
            r"下周|下个月|下月|下学期|生效|从|截至|截止|到.*为止", source)):
        # 课程模板暂不能直接表达起止限定；不能默认为本周就生效。
        gaps.append("effective_start")
    if rescheduling:
        if not result["date"] and not result["weekday"] and not time_only_update:
            gaps.append("date")
        if not result["start_time"]:
            gaps.append("start_time")
    result["missing"] = gaps
    if gaps:
        result["confidence"] = CONFIDENCE_MEDIUM
    return result


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
    from cdutetc import business_followup

    with closing(db._connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if not business_followup._allowed(conn, group_id, operator):
            return None
        course_id = db.create_training_session(group_id, info, operator=operator, conn=conn)
        fresh = business_followup._record(conn, group_id, "training_session", course_id)
        if fresh is None:
            conn.rollback()
        return fresh


def _save_contest(group_id: str, slots: dict, now: datetime, operator: str) -> dict | None:
    """写入比赛并绑定本群提醒范围；开赛前 15 分钟节点由既有同步逻辑生成。"""
    moment = _slot_date(slots, now)
    start = _parse_clock(slots.get("start_time"))
    if moment is None or not start:
        return None
    official_time = f"{moment:%Y-%m-%d} {start}"
    from cdutetc import business_followup

    try:
        with closing(db._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if not business_followup._allowed(conn, group_id, operator):
                return None
            cid = db.create_competition(
                {
                    "name": slots.get("title") or "比赛",
                    "official_time": official_time,
                    "official_site": _sanitize_url(slots.get("url")),
                },
                status="confirmed",
                operator=operator,
                conn=conn,
            )
            if not cid or db.set_competition_notify_groups(cid, {group_id}, operator, conn=conn) != 1:
                conn.rollback()
                return None
            if slots.get("location") and not business_followup.set_competition_location(
                    group_id, operator, cid, slots["location"], now=now, conn=conn):
                conn.rollback()
                return None
            fresh = business_followup._record(conn, group_id, "competition", cid)
            if fresh is None:
                conn.rollback()
            return fresh
    except Exception:
        logger.exception("老师意图写比赛失败")
        return None


def _apply_update(group_id: str, slots: dict, now: datetime, operator: str) -> tuple[str, dict] | None:
    """改期/改地点/取消：先定对象类型，再改字段，最后作废旧提醒节点。"""
    if not is_teacher_speaker(group_id, operator):
        return None
    target_id = int(slots.get("target_id") or 0)
    if target_id <= 0:
        return None
    cancel = bool(slots.get("cancel"))
    course = db.get_group_course(group_id, target_id)
    if course and course.get("status") != "confirmed":
        course = None
    contest = db.get_competition(target_id)
    contest = contest if (contest and contest.get("status") == "confirmed"
                          and group_id in db.get_competition_target_groups(target_id)) else None
    kind = slots.get("target_type") or ""
    if kind in {"course", "training_session"}:
        contest = None
    elif kind in {"contest", "competition"}:
        course = None
    elif kind or (course is not None and contest is not None):
        return None
    if course is not None:
        updates: dict = {}
        date = _parse_date(slots.get("date"), now)
        weekday = _parse_weekday(slots.get("weekday"))
        start = _parse_clock(slots.get("start_time"))
        end = _parse_clock(slots.get("end_time"))
        if weekday:
            updates["weekday"] = weekday
        if date and course.get("effective_start") and course.get("effective_end"):
            # 单次课程改日必须把生效窗口一起移动，周期模板仍保持长期生效。
            updates["weekday"] = _weekday_of(datetime.strptime(date, "%Y-%m-%d"))
            updates["effective_start"] = f"{date} 00:00"
            updates["effective_end"] = f"{date} 23:59"
        if start:
            updates["start_time"] = start
        if end:
            updates["end_time"] = end
        if slots.get("location"):
            updates["location"] = slots["location"]
        if slots.get("title"):
            updates["title"] = slots["title"]
        if not cancel and not updates:
            return None
        from cdutetc import business_followup

        with closing(db._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if not business_followup._allowed(conn, group_id, operator):
                return None
            current_course = business_followup._record(conn, group_id, "training_session", target_id)
            if current_course is None:
                return None
            if not kind and business_followup._record(conn, group_id, "competition", target_id) is not None:
                return None
            if cancel:
                saved = db.cancel_training_session(group_id, target_id, operator, conn=conn)
            else:
                saved = db.update_group_course(group_id, target_id, operator, conn=conn, **updates)
            if not saved:
                conn.rollback()
                return None
            # 课程、审计和旧节点共享事务，清理失败时也不提交改期/取消。
            db.drop_pending_deliveries("training_session", target_id, group_id=group_id, conn=conn)
            if cancel:
                return "course_cancelled", current_course
            fresh = business_followup._record(conn, group_id, "training_session", target_id)
            if fresh is None:
                conn.rollback()
                return None
            return "course_updated", fresh
    if contest is None:
        return None
    updates = {}
    moment = _slot_date(slots, now)
    start = _parse_clock(slots.get("start_time"))
    if moment is not None and start:
        updates["official_time"] = f"{moment:%Y-%m-%d} {start}"
    if slots.get("url"):
        updates["official_site"] = _sanitize_url(slots["url"])
    location = slots.get("location")
    if not cancel and not updates and not location:
        return None
    from cdutetc import business_followup, group_cleanup

    with closing(db._connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        # 领取写锁后再读权限、状态和群范围；字段与地点共享提交边界。
        if not business_followup._allowed(conn, group_id, operator):
            return None
        current_contest = business_followup._record(conn, group_id, "competition", target_id)
        if current_contest is None:
            return None
        if not kind and conn.execute("SELECT 1 FROM training_sessions WHERE id=? AND group_id=?"
                                     " AND status='confirmed'", (target_id, group_id)).fetchone():
            return None
        if cancel:
            if group_cleanup._sending(conn, group_id, "competition", target_id):
                return None
            path = group_cleanup._path(conn)
            group_cleanup._backup(path, path.parent / "backups")
            group_cleanup._clear(conn, group_id, "competition", target_id, operator,
                                 business_followup._now(now), "cancelled")
            return "contest_cancelled", current_contest
        # 任何一项失败都不允许提交已成功的另一项。
        if updates and not db.update_competition(target_id, updates, operator, "老师群内通知改期", conn=conn):
            conn.rollback()
            return None
        if location and not business_followup.set_competition_location(
                group_id, operator, target_id, location, now=now, conn=conn):
            conn.rollback()
            return None
        fresh = business_followup._record(conn, group_id, "competition", target_id)
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
                  "effective_start": "课程生效范围",
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
    if not is_teacher_speaker(group_id, sender_id):
        return _result(INTENT_CHAT_IGNORE, "ignored")
    if slots is None:
        return _result(INTENT_CHAT_IGNORE, "ignored")
    slots = _declared_schedule(text, slots, moment)
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
