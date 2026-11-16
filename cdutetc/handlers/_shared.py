"""命令层共享设施：回复与权限脚手架、字段/时间/地点解析器、搭便车提示。"""

import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from cdutetc import config, llm, database as db
from cdutetc.errors import AppErrorCode, safe_error_hint
from cdutetc.fetcher import fetch_text
from cdutetc.utils import is_super_admin, is_valid_account_id, normalize_time_expression, split_normalized_time

from cdutetc.handlers import contest_cmd as _contest

logger = logging.getLogger(__name__)


TZ = ZoneInfo("Asia/Shanghai")


logger = logging.getLogger(__name__)


def _error_reply(group_id: str, code: AppErrorCode, detail: str = "") -> list[dict]:
    text = f"[{code.value}] {detail}" if detail else safe_error_hint(code)
    return [{"group_id": group_id, "text": text, "error_code": code.value}]


# 只有「只读查询」类命令才搭车：它们不改变任何状态，尾部追加一行提示不会被
# 误读成操作结果；写操作（/报名、/课程添加 等）不搭车，避免语义混淆。
PIGGYBACK_COMMANDS = frozenset({
    "帮助", "help", "课程列表", "FAQ列表", "校安排", "通知列表",
    "比赛状态", "群状态", "提醒状态",
})


# 单群冷却：避免群员高频查询时每帧都刷同一句提示。
PIGGYBACK_COOLDOWN_SECONDS = 600


_piggyback_sent_at: dict[str, datetime] = {}


def reset_piggyback_cooldown() -> None:
    """清空搭便车冷却记录（单测与热重载用）。"""
    _piggyback_sent_at.clear()


def append_piggyback_notice(group_id: str, reply_text: str,
                            now: datetime | None = None) -> str:
    """被动回复尾部追加「最紧急的一条待办」；冷却中或无待办时原样返回。

    调用前提：本次回复走被动通道（reply_to 存在），因此不消耗主动额度。
    写库只发生在内存冷却表中，不触碰业务数据。
    """
    if not reply_text:
        return reply_text
    gid = (group_id or "").strip()
    if not gid:
        return reply_text
    current = now or datetime.now(TZ)
    current = current.replace(tzinfo=TZ) if current.tzinfo is None else current.astimezone(TZ)
    last = _piggyback_sent_at.get(gid)
    if last is not None and (current - last) < timedelta(seconds=PIGGYBACK_COOLDOWN_SECONDS):
        return reply_text
    from cdutetc import daily_digest

    notice = daily_digest.build_piggyback_notice(gid, current)
    if not notice:
        return reply_text
    _piggyback_sent_at[gid] = current
    return f"{reply_text.rstrip()}\n{notice}"


_HELP_HEADER = "【使用帮助】"


# 帮助文案在群里公开展示，只描述「能做什么」，不写实现细节。
# 每项格式：(所需身份, 命令, 说明)；身份 1=群成员 2=老师/超管 3=超管。
_HELP_LEVEL_MEMBER, _HELP_LEVEL_TEACHER, _HELP_LEVEL_SUPER = 1, 2, 3


_HELP_SECTIONS = (
    ("比赛", (
        (2, "@机器人 发布 名称和时间", "也可以直接发布官网链接"),
        (2, "/待确认", "查看等待确认的发布（旧写法 /待确认列表）"),
        (3, "/发布 名称和时间", "超管私聊录入比赛（绑定当前工作区群）"),
    )),
    ("学校安排", (
        (1, "/校安排", "查看学校统一安排"),
        (1, "/通知列表", "汇总本群近期的比赛、学校安排、课程和报名中的征集"),
    )),
    ("课程与训练", (
        (1, "/课程列表", "查看本群课程安排（带序号，可直接用于 /课程修改）"),
        (2, "/课程添加 <标题> [星期几] [开始-结束] [地点]", "添加课程"),
        (2, "/课程添加 <标题> 时间=周四 19点半-21点 地点=... 电脑=需要", "添加课程（命名参数写法）"),
        (2, "/课程取消 <id>", "取消课程"),
        (2, "/课程修改 <序号或课程名> [时间=...] [内容=...] [地点=...]", "修改课程（序号取自 /课程列表）"),
    )),
    ("征集登记", (
        (1, "/报名 <征集id> [字段=值 ...]", "报名或更新我的登记"),
        (1, "/报名 取消 <征集id>", "取消我的报名"),
        (1, "/征集汇总 <征集id>", "查看报名人数和剩余名额"),
        (2, "/征集 <标题> [截止时间] [名额] [字段1,字段2]", "发起征集"),
        (2, "/征集关闭 <征集id>", "停止报名"),
    )),
    ("提问", (
        (1, "@机器人 提问", "直接问，机器人会尽力回答"),
        (1, "/提问 <问题>", "记下问题，等老师回复"),
        (2, "/问题列表", "查看待回答的问题"),
        (2, "/回答 <编号> = <答案>", "回答指定编号的问题"),
    )),
    ("常见问题", (
        (2, "/FAQ列表 [关键词]", "查看本群常见问题"),
        (2, "/FAQ添加 <问题> = <答案>", "添加一条常见问题"),
        (2, "/FAQ删除 <id>", "删除一条常见问题"),
    )),
    ("群开关", (
        (2, "/群开关", "查看本群问答/比赛提醒/课程提醒/晨报开关状态"),
        (2, "/群开关 <名称> 开启|关闭", "调整本群单项主动能力，例如 /群开关 课程提醒 关闭"),
    )),
    ("提醒与来源", (
        (2, "/提醒列表 [天数]", "统一查看未来提醒（[比赛]/[课程]/[自定义]）与投递状态"),
        (2, "/提醒添加 <时间> <内容>", "添加自定义提醒，时间支持 2026-11-20 18:00 或 每周四 19:00"),
        (2, "/提醒修改 <序号> <新时间或新内容>", "修改自定义提醒，序号取自 /提醒列表"),
        (2, "/提醒删除 <序号>", "删除自定义提醒，序号取自 /提醒列表"),
        (2, "/提醒状态 [页码]", "查看未来7天的提醒时间和投递状态"),
        (2, "/比赛状态", "诊断本群比赛到点是否真会推送（就绪/阻断分列）"),
        (3, "/通知测试 [群|私聊] [目标标识]", "主动测试；官方群可在末尾加 Markdown 或 文本"),
        (2, "/抓取状态", "查看比赛来源更新情况"),
        (2, "/抓取一次", "立即检查一次比赛来源"),
    )),
    ("私聊会话（超管）", (
        (3, "/切换 <序号|群名>", "切换并记住工作区（旧写法 /选群）"),
        (3, "/当前群", "查看私聊当前工作区"),
        (3, "/重置群", "解除工作区锁定，恢复全局管理模式"),
        (3, "/晨报预览", "只在私聊回显该群晨报，不发送到群"),
    )),
    ("身份信息", (
        (2, "/群列表", "查看已授权的群和老师"),
        (2, "/老师列表", "查看已授权的群和老师"),
    )),
    ("比赛修正（超管）", (
        (3, "/改 <比赛id> <时间|链接|名称|赛道>", "免引号快捷修正，如 /改 12 11-29 09:00"),
        (3, "/修正 <比赛id> <字段=值 ...>", "按字段精确修正（旧写法，仍可用）"),
        (3, "/修正 <比赛id> 重新提取 <链接>", "重新读取官网信息"),
        (3, "/待审列表", "查看来源更新带来的改动"),
        (3, "/待审批准 <更新id>", "采纳来源更新"),
        (3, "/待审拒绝 <更新id> [原因]", "忽略来源更新"),
    )),
    ("群与老师管理（超管）", (
        (3, "/管理", "一屏总览：工作区、待确认发布、24小时内提醒、短命令速查（也可 /面板）"),
        (3, "/切换 <序号|群名>", "切换并记住工作区，重启后自动恢复，如 /切换 集训队"),
        (3, "/绑老师 老师姓名", "在当前工作区登记负责老师，如 /绑老师 孔老师"),
        (3, "/群配置 [群名或序号]", "查看该群比赛、负责老师和提醒；省略则看当前工作区"),
        (3, "/群提醒 [页码]", "查看当前工作区的提醒计划"),
        (3, "/周赛设置 开启|关闭", "只修改当前工作区的周赛开关"),
        (3, "/改 <编号> <时间|链接|名称|赛道>", "快捷修改赛程，如 /改 3 11-29 09:00 或 /改 3 官网链接"),
        (3, "/待确认", "查看等待确认的发布，回复 同意 <编号> / 拒绝 <编号>"),
        (3, "/加群 <群号或群名> [备注]", "让机器人在该群工作"),
        (3, "/删群 <群号或群名>", "取消该群授权"),
        (3, "/加老师 <QQ号> [备注]", "设置老师身份"),
        (3, "/删老师 <QQ号>", "取消老师身份"),
        (3, "/命名群 <群号或群名> <名称>", "给群起名字，提示里显示名字"),
        (3, "/命名人 <QQ号> <名称>", "给人起名字"),
        (3, "/命名列表", "查看已命名的群和人"),
        (3, "/认领此群 <QQ群号> <群备注名>", "在群里把本群与真实群号对上号"),
        (3, "/认领老师 <QQ号> <姓名>", "在群里认领老师身份，名字用于提示展示"),
        (3, "/通知群 <比赛id> <群号,群号>", "设置该比赛通知哪些群"),
        (3, "/比赛状态 全部", "私聊查看全部已授权群的提醒就绪情况"),
        (3, "/偷图 开|关", "按群开关零审核自动收图（自动过滤截图、去重、按上下文分类）"),
        (3, "/删图", "删掉本群最近一次机器人发的表情包，并拉黑该图不再收录"),
    )),
)


# 超管私聊 /帮助 顶部速查区：只列改版后的短命令，避免旧长命令干扰
_HELP_SUPER_QUICK = (
    "【超管快捷命令】",
    "总览：/管理（也可 /面板）",
    "换群：/切换 集训队（或 /切换 序号），重启后自动恢复",
    "老师：/绑老师 老师姓名",
    "改赛程：/改 编号 11-29 09:00，或 /改 编号 官网链接",
    "发布审批：私聊发比赛通知（或 /待确认），回复 同意 编号 / 拒绝 编号",
)


_HELP_SUPER_FOOTER = ("超管速查：/管理 总览｜/切换 集训队 锁定工作区｜/绑老师 老师姓名｜/改 编号 11-29 09:00\n"
                      "发布审批：私聊发比赛通知（或 /待确认），回复 同意 编号 / 拒绝 编号。")


_HELP_TOPICS = {
    "比赛": ("比赛",),
    "课程": ("课程与训练", "学校安排"),
    "报名": ("征集登记",),
    "问答": ("提问", "常见问题"),
    "提醒": ("提醒与来源",),
    "管理": ("身份信息", "群与老师管理（超管）", "私聊会话（超管）"),
    "修正": ("比赛修正（超管）",),
}


_HELP_TOPIC_ALIASES = {"征集": "报名", "训练": "课程", "FAQ": "问答", "faq": "问答",
                       "常见问题": "问答", "老师": "管理", "超管": "管理", "管理员": "管理",
                       "面板": "管理", "工作区": "管理"}


_HELP_EXAMPLES = {
    "比赛": ((2, "例：@机器人 发布 ACM校赛，明天下午3点开始"),
             (3, "发布审批：私聊发比赛通知（或 /待确认），回复 同意 编号 / 拒绝 编号。")),
    "课程": ((2, "例：/课程添加 算法训练 周三 19:00-21:00 机房"),),
    "报名": ((1, "例：/报名 3；编号看老师发出的征集通知，需要资料时再追加 姓名=张三。"),
             (2, "发起报名例：/征集 周末训练")),
    "问答": ((1, "例：@机器人 蓝桥杯什么时候报名？"),
             (2, "维护例：/FAQ添加 校赛在哪报名 = 在教务系统报名")),
    "提醒": ((3, "群通知测试例：/通知测试 群 Markdown"),),
    "修正": ((3, "例：/改 3 11-29 09:00；/改 3 官网链接；按字段：/修正 3 name=新的比赛名"),),
    "管理": ((3, "例：/切换 集训队 锁定工作区；/绑老师 孔老师；/改 3 11-29 09:00；/管理 看总览。"),
             (3, "解除锁定：/重置群（之后恢复全局管理模式）")),
}


_AMEND_FIELD_MAP = {
    "name": "name",
    "edition": "edition",
    "site": "official_site",
    "official_site": "official_site",
    "reg_start": "official_reg_start",
    "official_reg_start": "official_reg_start",
    "reg_deadline": "official_reg_deadline",
    "official_reg_deadline": "official_reg_deadline",
    "time": "official_time",
    "official_time": "official_time",
    "tracks": "tracks",
    "rules": "official_rules",
    "official_rules": "official_rules",
    "device_req": "device_req",
    "summary": "summary",
}


_AMEND_ASSIGNMENT = re.compile(r"([^\s=,，]+)\s*=")


_AMEND_NEXT_FIELD = re.compile(r"(?:\s+|[,，]\s*)(?=[^\s=,，]+\s*=)")


_AMEND_QUOTES = {'"': '"', "'": "'", "“": "”", "‘": "’"}


def _parse_field_values(text: str, field_map: dict[str, str] | None = None) -> dict[str, str]:
    """读取完整字段值；引号内的赋值片段、逗号和空格均属于正文。"""
    fields: dict[str, str] = {}
    pos = 0
    while pos < len(text):
        while pos < len(text) and (text[pos].isspace() or text[pos] in ",，"):
            pos += 1
        if pos == len(text):
            break
        assignment = _AMEND_ASSIGNMENT.match(text, pos)
        if assignment is None:
            raise ValueError("参数应为 字段=值；带赋值片段的文本请用引号包裹")
        key = assignment[1]
        field = field_map.get(key) if field_map is not None else key
        if field is None:
            raise ValueError(f"不支持的字段：{key}")
        if field in fields:
            raise ValueError(f"字段重复：{field}（含别名）")
        pos = assignment.end()
        value_start = pos
        quote_start = pos
        while quote_start < len(text) and text[quote_start].isspace():
            quote_start += 1
        closing_quote = _AMEND_QUOTES.get(text[quote_start]) if quote_start < len(text) else None
        if closing_quote:
            pos = quote_start + 1
            value: list[str] = []
            while pos < len(text):
                char = text[pos]
                if char == closing_quote:
                    pos += 1
                    break
                if char == "\\" and pos + 1 < len(text) and text[pos + 1] in (closing_quote, "\\"):
                    pos += 1
                    char = text[pos]
                value.append(char)
                pos += 1
            else:
                raise ValueError(f"字段 {key} 的引号未闭合")
            if pos < len(text) and not (text[pos].isspace() or text[pos] in ",，"):
                raise ValueError(f"字段 {key} 的引号后应为空白或逗号分隔的下一个字段")
            fields[field] = "".join(value)
        else:
            separator = _AMEND_NEXT_FIELD.search(text, value_start)
            pos = separator.start() if separator else len(text)
            fields[field] = text[value_start:pos].strip()
    return fields


# ---- 免引号快捷改期 / 改链接 / 改名 -------------------------------------
# 支持 `/改 <id> 11-29 09:00`、`/改 <id> 2026-11-29 09:00`、
# `/改 <id> 11月29日 9:00` 这类不带 key= 的写法，按值的形态智能判定字段。
_AMEND_INLINE_TIME = re.compile(
    r"^(?:(?P<year>\d{4})[-/.])?(?P<month>\d{1,2})[-/.月](?P<day>\d{1,2})日?\s*"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})$"
)


_AMEND_URL_PATTERN = re.compile(r"^https?://\S+$", re.IGNORECASE)


_AMEND_TRACK_SPLIT = re.compile(r"[、,，/]")


def _looks_like_tracks(value: str) -> bool:
    """「C++、Python」这类短项并列当赛道；长句仍按比赛名称处理。"""
    items = [item.strip() for item in _AMEND_TRACK_SPLIT.split(value) if item.strip()]
    return len(items) >= 2 and all(len(item) <= 12 for item in items)


def _broadcast_to_groups(competition_name: str, cid: int, text: str, exclude: str = "") -> list[dict]:
    """修正通知沿用该比赛的当前范围，不能绕过普通比赛或周赛的群隔离。"""
    return [
        {"group_id": gid, "text": text, "active_group": True}
        for gid in sorted(db.get_competition_target_groups(cid))
        if gid != exclude
    ]


async def _amend_refetch(group_id: str, cid: int, comp: dict, rest: str) -> list[dict]:
    """重新提取有效字段，并将真实抓取地址及正文基线与比赛同事务保存。"""
    url = rest[len("重新提取"):].strip()
    if not url:
        return [{"group_id": group_id, "text": "用法：/修正 <比赛id> 重新提取 <链接>"}]
    try:
        page = await fetch_text(url)
    except Exception:
        return _error_reply(group_id, AppErrorCode.FETCH)
    if not isinstance(page, str) or not page.strip():
        return [{"group_id": group_id, "text": "链接正文为空，未修正"}]
    try:
        info, err = await llm.extract_contest(f"链接正文：{page}\n超管修正补充：{rest}")
    except Exception:
        return [{"group_id": group_id, "text": "信息提取失败，未修正"}]
    if err is not None or not info:
        return [{"group_id": group_id, "text": f"信息提取失败：{err or '未知原因'}"}]
    validated, err = llm.validate_contest_info(info)
    if err:
        return [{"group_id": group_id, "text": f"信息提取失败：{err}"}]
    info = {**info, **validated}
    aliases = {"reg_deadline": "official_reg_deadline", "contest_time": "official_time", "raw_note": "official_rules"}
    aliases.update({field: field for field in _AMEND_FIELD_MAP.values()})
    fields = {
        target: info[key] for key, target in aliases.items() if key in info
        and info[key] is not None and not (isinstance(info[key], str) and not info[key].strip())
    }
    fields["official_site"] = url
    try:
        ok = db.amend_competition(
            cid, fields, operator="super_admin", note=f"重新提取：{url}", source_text=page,
        )
    except ValueError as e:
        return [{"group_id": group_id, "text": f"修正失败：{e}"}]
    if not ok:
        return [{"group_id": group_id, "text": f"比赛 #{cid} 不存在，无法修正"}]
    msgs = [{"group_id": group_id, "text": f"已重新提取并修正「{fields['name']}」（#{cid}），修正版本已生成"}]
    msgs += _broadcast_to_groups(fields["name"], cid, f"比赛「{fields['name']}」（#{cid}）官网信息已由超管更新，请以最新信息为准", exclude=group_id)
    return msgs


_COURSE_LIST_INDENT = "    "


_COURSE_LIST_MODIFY_TIP = "调整课程：/课程修改 <序号> 地点=... 要求=..."


_COURSE_LOCATION_PLACEHOLDERS = ("无", "待定", "未知")


def _course_location_display(location: str | None) -> str:
    """列表里的地点展示值：空 / 纯空白 / 「无」/「待定」统一渲染成「待定」。"""
    text = (location or "").strip()
    return "待定" if (not text or text in _COURSE_LOCATION_PLACEHOLDERS) else text


_NOTICE_WINDOW_DAYS = 30


_NOTICE_TIME_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M")


def _is_upcoming(now: datetime, *values: str | None) -> bool:
    """判断某条安排是否属于「近期」。

    任一时间落在 now～now+30 天内即算近期（含报名中）；时间全部缺失时保留，
    因为「时间待定」的安排仍需让群成员看到；有可解析时间但全部已过期则剔除。
    """
    horizon = now + timedelta(days=_NOTICE_WINDOW_DAYS)
    parsed = [t for t in (_contest._parse_notice_time(v) for v in values) if t is not None]
    if not parsed:
        return True
    return any(now <= t <= horizon for t in parsed)


# 课程参数键：「/课程添加」与「/课程修改」共用同一套解析口径，
# 用正则按 `键=值` 切分，值内部允许空格（如 `时间=周四 19点半-21点`）。
_COURSE_FIELD_KEYS = ("标题", "内容", "要求", "时间", "地点", "软件", "设备", "电脑")


_COURSE_FIELD_RE = re.compile(
    r"(" + "|".join(_COURSE_FIELD_KEYS) + r")\s*[=＝]\s*(.*?)"
    r"(?=\s+(?:" + "|".join(_COURSE_FIELD_KEYS) + r")\s*[=＝]|$)",
    re.S,
)


_QUOTE_PAIRS = (('"', '"'), ("'", "'"), ("“", "”"), ("「", "」"))


# 位置参数里的时间表达：`周四 19:00-21:00`、`周四 19-21点`、`19点半-21点`。
# 只有形如「时:分 / N点(半)」的时分写法才被当成时间，避免把 `3-5人` 之类误抽。
_COURSE_WEEKDAY_TOKEN = r"(?:周|星期)[一二三四五六日天]"


_COURSE_CLOCK_TOKEN = r"\d{1,2}(?::\d{2}|点(?:\d{1,2}分?|半)?)?"


_COURSE_SPAN_TOKEN = rf"{_COURSE_CLOCK_TOKEN}\s*(?:[-~～—－]|到|至)\s*{_COURSE_CLOCK_TOKEN}"


_COURSE_TIME_SPAN_RE = re.compile(
    rf"(?:{_COURSE_WEEKDAY_TOKEN}\s*(?:{_COURSE_SPAN_TOKEN}|{_COURSE_CLOCK_TOKEN}))"
    rf"|{_COURSE_SPAN_TOKEN}"
)


_TRAINING_ADD_USAGE = (
    "用法：/课程添加 <标题> [时间=周四 19:00-21:00] [地点=二教B201] [要求=带电脑]"
    "（标题只写课程名；要求=/内容= 或尾随文本写入「要求」，"
    "过长时自动智能提炼）"
)


_TRAINING_MODIFY_USAGE = (
    "用法：/课程修改 <序号或课程名> [时间=周四 19:00-21:00] [标题=新标题] [要求=新要求]"
    "（标题= 只改课程名；要求=/内容=/尾随文本 只改要求，互不覆盖）"
)


def _strip_quotes(value: str) -> str:
    """去掉值两侧的包裹引号（中文引号也兼容）。"""
    text = (value or "").strip()
    for left, right in _QUOTE_PAIRS:
        if len(text) >= 2 and text.startswith(left) and text.endswith(right):
            return text[1:-1].strip()
    return text


# 地点字段口径：只放地点本身。历史上有两处污染来源——位置参数把「要求」长文挤进地点位、
# 以及人工把整段规则写成地点——两者都会让列表排版崩掉，所以在入库前统一收敛。
# 注意「待定」不在清空词里：它就是列表兜底展示值，写了也不该被当成删除指令。
_LOCATION_CLEAR_WORDS = frozenset({"清空", "删除", "清除", "去掉", "无", "空"})


_LOCATION_REQUIREMENT_PREFIXES = ("要求", "课程要求", "内容", "备注", "注意", "提醒", "须知")


_LOCATION_RULE_HINTS = (
    "要求", "禁止", "必须", "自带", "自备", "需带", "请", "记得", "提前",
    "不要", "电脑", "笔记本", "作业", "签到", "考勤",
)


_LOCATION_MAX_LEN = 24


def _normalize_location_text(raw: str | None) -> str:
    """去掉包裹引号、压缩空白，得到地点原始值（不做清空 / 污染判断）。"""
    return " ".join(_strip_quotes((raw or "").strip()).split())


def _is_location_cleared(text: str) -> bool:
    """`地点=清空 / 地点=无 / 地点=""` 一律视为清空已有地点。"""
    return not text or text in _LOCATION_CLEAR_WORDS


def _looks_like_requirement(text: str) -> bool:
    """判断「地点」值是否其实是一段课程要求（防污染 location 字段）。"""
    if not text:
        return False
    if text.startswith(_LOCATION_REQUIREMENT_PREFIXES):
        return True
    # 长文本、多句、带规则动词的，都当成要求而不是地点。
    if len(text) > _LOCATION_MAX_LEN or any(ch in text for ch in "\n。；;！!？?"):
        return True
    return any(hint in text for hint in _LOCATION_RULE_HINTS)


def _split_course_fields(args: str) -> tuple[dict[str, str], str]:
    """抽出 `键=值` 参数，返回（字段字典，剩余位置参数文本）。

    剩余文本仍按原有位置参数规则解析，保证老的 `/课程添加 标题 周四 19-21点 地点`
    写法完全兼容。
    """
    text = args or ""
    fields: dict[str, str] = {}
    for m in _COURSE_FIELD_RE.finditer(text):
        key = m.group(1)
        if key in fields:
            continue
        fields[key] = _strip_quotes(m.group(2))
    rest = _COURSE_FIELD_RE.sub(" ", text).strip() if fields else text.strip()
    return fields, " ".join(rest.split())


def _apply_time_fields(raw: str) -> dict[str, str]:
    """归一化「周几 + 时间」并拆成 weekday/start_time/end_time（识别不到的键不返回）。"""
    weekday, start_time, end_time = split_normalized_time(normalize_time_expression(raw or ""))
    out: dict[str, str] = {}
    if weekday:
        out["weekday"] = weekday
    if start_time:
        out["start_time"] = start_time
    if end_time:
        out["end_time"] = end_time
    return out


def _fallback_time_fields(weekday: str, time_range: str) -> dict[str, str]:
    """归一化失败时保留用户原文，避免把录入内容吞掉。"""
    out: dict[str, str] = {}
    text = (time_range or "").strip()
    if text:
        for sep in ("-", "~", "～", "—"):
            if sep in text:
                st, et = text.split(sep, 1)
                out["start_time"], out["end_time"] = st.strip(), et.strip()
                break
        else:
            out["start_time"] = text
    if weekday:
        out["weekday"] = weekday.strip()
    return out


def _extract_time_span(text: str) -> tuple[dict[str, str], str]:
    """从位置参数里抽出时间表达，返回（时间字段，去掉时间后的剩余文本）。

    `/课程修改 1 周四 19:00-21:00`、`/课程修改 1 19点半-21点 加练` 这类写法靠它
    把「时间」与「标题/要求」拆开；识别不到时间时原样返回，由调用方按标题处理。
    """
    raw = " ".join((text or "").split())
    if not raw:
        return {}, ""
    match = _COURSE_TIME_SPAN_RE.search(raw)
    if match is None:
        return {}, raw
    # 没有「:」「点」「周几」的纯数字区间（如 3-5）不当时间，避免吞掉标题。
    if not re.search(r"[:：点]|周|星期", match.group(0)):
        return {}, raw
    time_fields = _apply_time_fields(match.group(0))
    if not time_fields:
        return {}, raw
    leftover = " ".join((raw[: match.start()] + " " + raw[match.end():]).split())
    return time_fields, leftover


def _format_course_time(weekday: str, start_time: str, end_time: str) -> str:
    """拼「周X HH:MM-HH:MM」展示串，缺失部分自动省略。"""
    if start_time and end_time:
        span = f"{start_time}-{end_time}"
    elif start_time:
        span = start_time
    elif end_time:
        span = f"-{end_time}"
    else:
        span = ""
    return " ".join(x for x in ((weekday or "").strip(), span) if x)


def _prepare_requirement(raw_text: str, title: str, category: str = "课程") -> tuple[str, bool]:
    """把「要求」文本准备成入库值：过长时先做 LLM 智能提炼，失败降级原文。

    返回（入库文本, 是否经过提炼）。提炼不可用时绝不能阻断入库。
    """
    text = (raw_text or "").strip()
    if not text:
        return "", False
    try:
        return llm.summarize_entry_with_llm(category, title, text)
    except Exception as exc:
        logger.warning("要求提炼异常，按原文入库：%s", exc)
        return text, False


def _match_course_by_title(sessions: list[dict], text: str) -> tuple[dict | None, str]:
    """按课程名匹配：先精确（去空格），再退化到包含匹配，多重命中报歧义。"""
    target = (text or "").strip().replace(" ", "")
    if not target:
        return None, ""
    exact = [s for s in sessions if (s.get("title") or "").replace(" ", "") == target]
    if len(exact) == 1:
        return exact[0], ""
    if len(exact) > 1:
        return None, f"课程名「{text}」匹配到多门课程，请改用序号"
    fuzzy = [s for s in sessions if target in (s.get("title") or "").replace(" ", "")]
    if len(fuzzy) == 1:
        return fuzzy[0], ""
    if len(fuzzy) > 1:
        return None, f"课程名「{text}」匹配到多门课程，请改用序号"
    return None, ""


def _resolve_course_target(group_id: str, target: str) -> tuple[dict | None, str]:
    """解析课程目标：列表序号 → #id/课程 id → 课程名。"""
    sessions = db.list_training_sessions(group_id)
    text = (target or "").strip()
    if text.isdigit():
        index = int(text)
        if 1 <= index <= len(sessions):
            return sessions[index - 1], ""
        course = db.get_group_course(group_id, index)
        return (course, "") if course else (None, "")
    if text.startswith("#") and text[1:].strip().isdigit():
        course = db.get_group_course(group_id, int(text[1:].strip()))
        return (course, "") if course else (None, "")
    return _match_course_by_title(sessions, text)


def _resolve_course_target_with_span(group_id: str, text: str) -> tuple[dict | None, str, int]:
    """解析课程目标，返回（课程, 歧义提示, 消耗的 token 数）。

    课程名允许带空格（如「算法 特训」），所以先按 1→4 个 token 找精确同名课程，
    取最长的精确命中；都不精确命中时退回单 token 的序号 / #id / 模糊匹配。
    """
    tokens = (text or "").split()
    if not tokens:
        return None, "", 0
    head = tokens[0]
    if head.isdigit() or (head.startswith("#") and head[1:].strip().isdigit()):
        course, ambiguous = _resolve_course_target(group_id, head)
        return course, ambiguous, 1
    sessions = db.list_training_sessions(group_id)
    first_ambiguous = ""
    for size in range(1, min(4, len(tokens)) + 1):
        candidate = " ".join(tokens[:size])
        flat = candidate.replace(" ", "")
        matched = [s for s in sessions if (s.get("title") or "").replace(" ", "") == flat]
        if len(matched) == 1:
            return matched[0], "", size
        if len(matched) > 1:
            return None, f"课程名「{candidate}」匹配到多门课程，请改用序号", size
        if not first_ambiguous:
            _, ambiguous = _match_course_by_title(sessions, candidate)
            if ambiguous:
                first_ambiguous = ambiguous
    course, ambiguous = _resolve_course_target(group_id, head)
    if course is not None:
        return course, "", 1
    return None, ambiguous or first_ambiguous, 1


def _label_with_kind(kind: str, group_id: str, sender: str, args: str) -> list[dict]:
    if not is_super_admin(sender):
        return [{"group_id": group_id, "text": "仅超管可设置名称"}]
    parts = (args or "").strip().split(None, 1)
    if len(parts) < 2:
        cmd = "命名群" if kind == "group" else "命名人"
        return [{"group_id": group_id,
                 "text": f"用法：/{cmd} <标识> <名称>\n例：/{cmd} A0EF5D2D… 集训队群"}]
    account_id, label = parts[0].strip(), parts[1].strip()
    if not is_valid_account_id(account_id):
        return [{"group_id": group_id, "text": "标识格式不对"}]
    try:
        db.set_account_label(kind, account_id, label, operator=sender)
    except ValueError as e:
        return [{"group_id": group_id, "text": str(e)}]
    what = "群" if kind == "group" else "账号"
    short = f"{account_id[:8]}…" if len(account_id) > 12 else account_id
    return [{"group_id": group_id, "text": f"已命名{what} {short} 为「{label}」"}]


_ANSWER_RE = re.compile(r"^\s*#?(\d+)\s*(?:=|＝|：|:|\s)\s*(.+)$", re.S)


SCOPE_DENIED_GROUP = "⚠️ 该命令仅限本群老师或超级管理员执行"


SCOPE_DENIED_PRIVATE = "⛔ 私聊管理仅对超级管理员开放，请在已授权群内使用命令"


# 其余既有管理命令沿用各自的权限实现，避免改动已知行为。
_STATUS_COMMANDS = frozenset({"比赛状态", "群状态"})


def _allowed_group_ids() -> list[str]:
    return sorted(config.ALLOWED_GROUPS)


def _group_label(group_id: str) -> str:
    from cdutetc import reminder_preflight
    return reminder_preflight.group_label(group_id)


def _panel_time(moment: datetime) -> str:
    local = moment.astimezone(TZ) if moment.tzinfo else moment
    return local.strftime("%m-%d %H:%M")


def _group_alias_parts(gid: str) -> tuple[str, str]:
    """返回群的（备注名, 真实群号）；未认领或库不可用时都返回空串。"""
    import sqlite3

    try:
        alias = db.get_entity_alias("group", gid) or {}
        name = (alias.get("display_name") or "").strip()
        numeric = (alias.get("numeric_id") or "").strip()
        if not name:
            # 兼容只走过 /命名群 的历史数据
            name = (db.get_account_label("group", gid) or "").strip()
    except sqlite3.Error:
        return "", ""
    return name, numeric


_WEEKDAY_INDEX = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


_WEEKLY_PREFIX = re.compile(
    r"^(?:每周|每星期|每礼拜|周|星期|礼拜)([一二三四五六日天])\s*"
    r"(\d{1,2})(?:[:：点时](\d{1,2}))?分?"
)


_ABSOLUTE_PREFIX = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2})[:：点时](\d{1,2})")


def _upcoming_entries(group_id: str, now: datetime, days: int = 7) -> list[dict]:
    from cdutetc import reminder as reminder_module

    return reminder_module.build_upcoming_reminders(group_id, now, days=days)


def _resolve_custom_reminder(group_id: str, selector: str, now: datetime) -> dict:
    """把「序号（取自 /提醒列表）」或「#编号」解析成本群的自定义提醒。"""
    raw = str(selector or "").strip()
    if not raw:
        raise ValueError("请提供序号，序号取自 /提醒列表")
    if raw.startswith("#"):
        try:
            reminder_id = int(raw.lstrip("#"))
        except (TypeError, ValueError):
            raise ValueError("编号格式应为 #12") from None
        existing = db.get_custom_reminder(reminder_id)
        if not existing or existing.get("group_id") != group_id:
            raise ValueError(f"本群没有编号为 {reminder_id} 的自定义提醒")
        return existing
    try:
        index = int(raw)
    except (TypeError, ValueError):
        raise ValueError("请提供序号，序号取自 /提醒列表") from None
    entries = _upcoming_entries(group_id, now, 7)
    if not 1 <= index <= len(entries):
        raise ValueError(f"序号超出范围，本群未来 7 天共有 {len(entries)} 条提醒")
    entry = entries[index - 1]
    if entry.get("source") != "custom":
        from cdutetc import reminder as reminder_module

        label = reminder_module.SOURCE_LABELS.get(entry.get("source"), entry.get("source"))
        raise ValueError(f"第 {index} 条是 [{label}] 提醒，只能修改自定义提醒")
    existing = db.get_custom_reminder(int(entry["obj_id"]))
    if not existing:
        raise ValueError(f"提醒 #{entry['obj_id']} 已不存在")
    return existing
