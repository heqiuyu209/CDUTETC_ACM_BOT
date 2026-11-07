"""明确比赛发布语句的本地解析；复杂公告仍交给受校验的提取器。"""

from datetime import datetime, timedelta
import re
import unicodedata
from zoneinfo import ZoneInfo

from cdutetc import config

TZ = ZoneInfo(config.TZ_NAME)
_NUMBER = r"(?:\d+|[零〇一二两三四五六七八九十]{1,3})"
_CLOCK = re.compile(
    rf"(?<!\d)(?P<period>凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|晚间)?\s*"
    rf"(?P<hour>{_NUMBER})\s*(?:"
    rf"[点时](?:(?P<part>半|一刻|三刻)|\s*(?P<minute>{_NUMBER})\s*分?)?"
    r"|:(?P<colon_minute>\d+))"
)
_DATE = re.compile(r"(?<!\d)(?P<year>\d{4})[年/.-](?P<month>\d{1,2})[月/.-](?P<day>\d{1,2})[日号]?(?!\d)")
_MONTH_DAY = re.compile(r"(?P<month>\d{1,2})月(?P<day>\d{1,2})[日号]?")
_RELATIVE = re.compile(r"今天|今日|今早|今晚|明天|明日|明早|明晚|后天|昨天|昨日|前天")
_WEEKDAY = re.compile(r"(?P<week>下周|本周|这周|周|星期|礼拜)(?P<day>[一二三四五六日天])")
_QUERY = re.compile(r"[?？]|什么时候|何时|几点|几号|怎么|如何|什么|是否|有没有|有没|能不能|能否|吗[呀啊呢。！!~～]*$")
_NEGATED_ACTION = re.compile(r"不要|别(?:再)?(?:发布|添加|登记)|取消|撤销|不(?:是|会|要).*比赛")
_EVENT = re.compile(r"比赛|周赛|校赛|训练赛|icpc|ccpc|蓝桥杯|天梯赛", re.I)


def is_publication_question(text: str) -> bool:
    return bool(_QUERY.search(text) or _NEGATED_ACTION.search(text))


def is_timed_contest_announcement(text: str) -> bool:
    """有具体钟点的比赛开始陈述，不能用查询模型代替真实发布流程。"""
    text = unicodedata.normalize("NFKC", text)
    return bool(not is_publication_question(text) and _CLOCK.search(text) and _EVENT.search(text)
                and re.search(r"开始|开赛|开打|举行|进行|提醒我", text))


def is_timed_reminder_request(text: str) -> bool:
    return bool(is_timed_contest_announcement(text) and re.search(r"(?:记得|记住|帮我|给我|请|你).*提醒|提醒我", text))


def _number(text: str) -> int:
    if text.isdigit():
        return int(text)
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
              "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if "十" in text:
        left, right = text.split("十", 1)
        return (digits[left] if left else 1) * 10 + (digits[right] if right else 0)
    return int("".join(str(digits[char]) for char in text))


def _reference(now: datetime | None) -> datetime:
    now = now or datetime.now(TZ)
    return now.replace(tzinfo=TZ) if now.tzinfo is None else now.astimezone(TZ)


def parse_local_time(text: str, now: datetime | None = None) -> tuple[str, str] | None:
    """只接受一个明确钟点；非法日期报错，多个时间或相对天数不猜。"""
    text = unicodedata.normalize("NFKC", text)
    clocks = list(_CLOCK.finditer(text))
    if len(clocks) != 1:
        return None
    clock = clocks[0]
    dates = list(_DATE.finditer(text))
    relatives = list(_RELATIVE.finditer(text))
    if len(dates) > 1 or len(relatives) > 1 or (dates and relatives):
        return None
    if re.search(r"明年|后年|去年|下个月|下月|下学期|左右|大约|待定|待通知|不确定", text):
        return None
    if (re.search(r"下周|本周|这周|周末", text) and not _WEEKDAY.search(text)) or (
        re.search(r"\d+年", text) and not dates
    ):
        return None
    current = _reference(now)
    notes = ""
    try:
        hour = _number(clock["hour"])
        minute = _number(clock["minute"] or clock["colon_minute"] or "0")
        if clock["part"]:
            minute = {"半": 30, "一刻": 15, "三刻": 45}[clock["part"]]
        period = clock["period"] or ""
        if not period and relatives:
            period = "晚上" if relatives[0][0] in ("今晚", "明晚") else ("早上" if relatives[0][0] in ("今早", "明早") else "")
        next_midnight = False
        if period:
            if not (1 <= hour <= 12 or period == "凌晨" and hour == 0 or period == "下午" and 13 <= hour <= 23):
                raise ValueError("钟点与时段不一致")
            if period in ("晚上", "晚间") and hour == 12:
                hour, next_midnight = 0, True
            elif period in ("下午", "傍晚", "晚上", "晚间") and hour < 12:
                hour += 12
            elif period == "凌晨" and hour == 12:
                hour = 0
            elif period == "中午":
                if hour in (1, 2):
                    hour += 12
                elif hour not in (11, 12):
                    raise ValueError("中午的钟点有歧义")
        target_date = current.date()
        if dates:
            date = dates[0]
            target_date = datetime(int(date["year"]), int(date["month"]), int(date["day"])).date()
        elif relatives:
            offset = {"明天": 1, "明日": 1, "明早": 1, "明晚": 1, "后天": 2,
                      "昨天": -1, "昨日": -1, "前天": -2}.get(relatives[0][0], 0)
            target_date += timedelta(days=offset)
        elif date := _MONTH_DAY.search(text):
            target_date = datetime(current.year, int(date["month"]), int(date["day"])).date()
            notes = f"未写年份，按{current.year}年解析。"
        elif weekday := _WEEKDAY.search(text):
            day = "一二三四五六日".index(weekday["day"].replace("天", "日"))
            delta = day - current.weekday()
            if weekday["week"] == "下周":
                delta += 7
            elif weekday["week"] not in ("本周", "这周"):
                delta %= 7
            target_date += timedelta(days=delta)
        else:
            notes = "未写日期，按今天（北京时间）解析。"
        if next_midnight:
            target_date += timedelta(days=1)
            notes += "晚上12点按次日00点解析。"
        target = datetime(target_date.year, target_date.month, target_date.day, hour, minute, tzinfo=TZ)
    except (ValueError, KeyError, OverflowError) as error:
        raise ValueError("日期或钟点无效，请核对比赛时间") from error
    return target.strftime("%Y-%m-%d %H:%M"), notes


def parse_simple_publication(text: str, now: datetime | None = None) -> dict | None:
    """时间加名称的简短发布零模型解析；其他字段或多时间公告不走捷径。"""
    text = unicodedata.normalize("NFKC", text).strip()
    if is_publication_question(text) or re.search(r"https?://|报名|截止|地点|机房|教室|规则|要求|名额|费用|设备|联系", text):
        return None
    parsed = parse_local_time(text, now)
    if parsed is None:
        return None
    target, note = parsed
    name = _CLOCK.sub("", text)
    for pattern in (_DATE, _MONTH_DAY, _RELATIVE, _WEEKDAY):
        name = pattern.sub("", name)
    name = re.sub(r"现在做一个测试|现在测试一下|你记得提醒我|记得提醒我|提醒我|请提醒|帮我提醒|会有一[个场]比赛|有一[个场]比赛", "", name)
    name = re.sub(r"^(?:将于|定于|将在|于|添加一[个场]比赛|添加比赛|发布)[\s，,。:：]*", "", name)
    name = re.sub(r"正式开始|即将开始|开始|开赛|开打|举行|进行", "", name).strip(" \t\r\n，,。:：、；;！!~～")
    if name in ("", "一个比赛", "一场比赛", "比赛"):
        name = "临时比赛"
        note += "未提供比赛名，暂名“临时比赛”，请在确认时核对。"
    elif len(name) > 60 or re.search(r"[,，。；;!！:：]", name) or not _EVENT.search(name):
        return None
    info = {"name": name, "contest_time": target}
    if note:
        info["summary"] = note
    return info


def normalize_local_time_value(text: str, now: datetime | None = None) -> str | None:
    """时间字段只允许日期/时刻词，不从夹带任意正文的字符串抽时间。"""
    remaining = unicodedata.normalize("NFKC", text)
    for pattern in (_CLOCK, _DATE, _MONTH_DAY, _RELATIVE, _WEEKDAY):
        remaining = pattern.sub("", remaining)
    if remaining.strip(" \t\r\n，,。:："):
        return None
    parsed = parse_local_time(text, now)
    return parsed[0] if parsed else None
