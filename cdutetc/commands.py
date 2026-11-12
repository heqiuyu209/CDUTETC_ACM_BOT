"""命令处理模块（v2）：命令式操作 + 权限校验 + 全部业务命令。

行为边界见 PRODUCT_SPEC.md；命令与配置用法见 README.md。
- 命令式敏感操作 + 清晰帮助：所有写操作必须以命令形式显式发出，禁止隐式触发。
- 权限默认全拒绝：super_admin=超管（/修正、全局管理）；teacher=老师
  （课程/FAQ、征集管理）；member=普通成员只可报名/查询。
- 跨群隔离：命令处理始终携带 group_id / sender_id，数据库层按群过滤。
- 返回值统一为消息列表：[{"group_id": str, "text": str}, ...]，
  由上层（main.py）负责实际发送，本模块不接触任何 QQ SDK / 日志冒充发送。
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from cdutetc import config
from cdutetc.clients.base import capabilities_of
from cdutetc import content_filter
from cdutetc import crawler
from cdutetc import database as db
from cdutetc import faq as faq_search
from cdutetc import llm
from cdutetc import roles
from cdutetc.fetcher import fetch_text
from cdutetc.errors import AppErrorCode, safe_error_hint
from cdutetc.utils import (
    can_manage_group,
    is_super_admin,
    is_valid_account_id,
    normalize_time_expression,
    parse_command,
    split_normalized_time,
)

TZ = ZoneInfo("Asia/Shanghai")
logger = logging.getLogger(__name__)


def _error_reply(group_id: str, code: AppErrorCode, detail: str = "") -> list[dict]:
    text = f"[{code.value}] {detail}" if detail else safe_error_hint(code)
    return [{"group_id": group_id, "text": text, "error_code": code.value}]

# ---------------------------------------------------------------------
# 被动搭便车（piggyback）：只读查询的被动回复尾部追加一条最紧急待办
# ---------------------------------------------------------------------

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
        (2, "/待确认列表", "查看等待确认的发布"),
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
    ("提醒与来源", (
        (2, "/提醒状态 [页码]", "查看未来7天的提醒时间和投递状态"),
        (2, "/比赛状态", "诊断本群比赛到点是否真会推送（就绪/阻断分列）"),
        (3, "/通知测试 [群|私聊] [目标标识]", "主动测试；官方群可在末尾加 Markdown 或 文本"),
        (2, "/抓取状态", "查看比赛来源更新情况"),
        (2, "/抓取一次", "立即检查一次比赛来源"),
    )),
    ("私聊调试（超管）", (
        (3, "/选群 [序号|群名|群号]", "私聊绑定目标群，之后私聊指令都作用于该群"),
        (3, "/当前群", "查看私聊当前绑定的目标群"),
        (3, "/重置群", "解除绑定，恢复全局管理模式"),
        (3, "/晨报预览", "只在私聊回显该群晨报，不发送到群"),
    )),
    ("身份信息", (
        (2, "/群列表", "查看已授权的群和老师"),
        (2, "/老师列表", "查看已授权的群和老师"),
    )),
    ("比赛修正（超管）", (
        (3, "/修正 <比赛id> <字段=值 ...>", "修正已发布的比赛信息"),
        (3, "/修正 <比赛id> 重新提取 <链接>", "重新读取官网信息"),
        (3, "/待审列表", "查看来源更新带来的改动"),
        (3, "/待审批准 <更新id>", "采纳来源更新"),
        (3, "/待审拒绝 <更新id> [原因]", "忽略来源更新"),
    )),
    ("群与老师管理（超管）", (
        (3, "/群配置 <群名或标识>", "私聊查看该群比赛、负责老师和提醒"),
        (3, "/群提醒 <群名或标识> [页码]", "私聊查看指定群的提醒计划"),
        (3, "/周赛设置 <群名或标识> 开启|关闭", "只修改这个群的周赛开关"),
        (3, "/群老师 <群名或标识> [老师标识,老师标识|清空]", "登记本群负责老师"),
        (3, "/加群 <群标识> [备注]", "让机器人在该群工作"),
        (3, "/删群 <群标识>", "取消该群授权"),
        (3, "/加老师 <QQ号> [备注]", "设置老师身份"),
        (3, "/删老师 <QQ号>", "取消老师身份"),
        (3, "/命名群 <标识> <名称>", "给群起名字，提示里显示名字而不是 openid"),
        (3, "/命名人 <标识> <名称>", "给人起名字"),
        (3, "/命名列表", "查看已命名的群和人"),
        (3, "/认领此群 <QQ群号> <群备注名>", "在群里把本群 openid 与真实群号对上号"),
        (3, "/认领老师 <QQ号> <姓名>", "在群里认领老师身份，名字用于提示展示"),
        (3, "/通知群 <比赛id> <群号,群号>", "设置该比赛通知哪些群"),
        (3, "/比赛状态 全部", "私聊查看全部已授权群的提醒就绪情况"),
    )),
)

_HELP_SUPER_FOOTER = "私聊审批：先 /待确认列表，再回复 同意 编号 或 拒绝 编号。\n私聊用 /群配置 群名 查看指定群；其他管理命令仍作用于默认管理群。"

_HELP_TOPICS = {
    "比赛": ("比赛",),
    "课程": ("课程与训练", "学校安排"),
    "报名": ("征集登记",),
    "问答": ("提问", "常见问题"),
    "提醒": ("提醒与来源",),
    "管理": ("身份信息", "群与老师管理（超管）"),
    "修正": ("比赛修正（超管）",),
}
_HELP_TOPIC_ALIASES = {"征集": "报名", "训练": "课程", "FAQ": "问答", "faq": "问答",
                       "常见问题": "问答", "老师": "管理", "超管": "管理", "管理员": "管理"}
_HELP_EXAMPLES = {
    "比赛": ((2, "例：@机器人 发布 ACM校赛，明天下午3点开始"),
             (3, "确认发布：私聊 /待确认列表，再回复 同意 编号。")),
    "课程": ((2, "例：/课程添加 算法训练 周三 19:00-21:00 机房"),),
    "报名": ((1, "例：/报名 3；编号看老师发出的征集通知，需要资料时再追加 姓名=张三。"),
             (2, "发起报名例：/征集 周末训练")),
    "问答": ((1, "例：@机器人 蓝桥杯什么时候报名？"),
             (2, "维护例：/FAQ添加 校赛在哪报名 = 在教务系统报名")),
    "提醒": ((3, "群通知测试例：/通知测试 群 Markdown"),),
    "修正": ((3, "改名称示例：/修正 3 name=新的比赛名"),),
}


def _role_level(member_id: str | None) -> int:
    if is_super_admin(member_id):
        return _HELP_LEVEL_SUPER
    if can_manage_group(member_id):
        return _HELP_LEVEL_TEACHER
    return _HELP_LEVEL_MEMBER


def build_help(member_id: str | None, topic: str = "") -> str:
    """默认给简短入口，指定主题时再显示对应身份的用法。"""
    level = _role_level(member_id)
    topic = topic.strip()
    topic = _HELP_TOPIC_ALIASES.get(topic, topic)
    if not topic:
        lines = [_HELP_HEADER,
                 "查比赛、问事情：在群里 @我 + 你的问题",
                 "看课程：/课程列表",
                 "报名：/报名 编号",
                 "详细用法：",
                 "/帮助 比赛｜/帮助 课程｜/帮助 报名｜/帮助 问答"]
        if level >= _HELP_LEVEL_TEACHER:
            lines.extend(("提醒检查：/帮助 提醒", "群和老师管理：/帮助 管理"))
        if level >= _HELP_LEVEL_SUPER:
            lines.append("发布审批：私聊 /待确认列表，再回复 同意 编号")
        return "\n".join(lines)
    if topic != "全部" and topic not in _HELP_TOPICS:
        return "帮助主题：比赛、课程、报名、问答；老师/超管还可看 提醒、管理，超管可看 修正。\n例如：/帮助 比赛"
    selected = _HELP_TOPICS.get(topic)
    lines = [f"【{topic}用法】"]
    for title, rows in _HELP_SECTIONS:
        if selected is not None and title not in selected:
            continue
        visible = [(cmd, desc) for need, cmd, desc in rows if need <= level]
        if not visible:
            continue
        if topic == "全部":
            lines.append(f"【{title}】")
        for cmd, desc in visible:
            lines.append(f"{cmd} → {desc}")
    if len(lines) == 1:
        return "该主题没有你当前可用的命令，发送 /帮助 查看常用功能。"
    lines.extend(example for need, example in _HELP_EXAMPLES.get(topic, ()) if need <= level)
    if level >= _HELP_LEVEL_SUPER and topic in ("管理", "全部"):
        lines.append(_HELP_SUPER_FOOTER)
    lines.append("返回：/帮助；完整命令：/帮助 全部")
    return "\n".join(lines)


# 兼容旧引用：默认按普通成员展示（不含管理命令）
HELP_TEXT = build_help(None)

# ---------------------------------------------------------------------
# v2 命令：修正（超管）
# ---------------------------------------------------------------------

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


def _parse_amend_fields(text: str) -> dict[str, str]:
    return _parse_field_values(text, _AMEND_FIELD_MAP)


def _broadcast_to_groups(competition_name: str, cid: int, text: str, exclude: str = "") -> list[dict]:
    """向全部允许群广播一条通知（排除触发群可选）。订阅体系已移除，修正/发布影响全体群。"""
    return [
        {"group_id": gid, "text": text}
        for gid in sorted(config.ALLOWED_GROUPS)
        if gid != exclude
    ]


async def cmd_amend(group_id: str, sender: str, args: str) -> list[dict]:
    """超管修正已发布比赛：字段纠正或重新提取官网，生成修正版本并通知各群。

    用法：
      /修正 <比赛id> <字段=值 ...>      纠正指定字段
      /修正 <比赛id> 重新提取 <链接>    重新抓取官网并提取，覆盖官方字段
    """
    if not is_super_admin(sender):
        return [{"group_id": group_id, "text": "仅超管可修正比赛信息 [permission]", "error_code": AppErrorCode.PERMISSION.value}]
    parts = args.strip().split(None, 1)
    if not parts:
        return [{"group_id": group_id, "text": "用法：/修正 <比赛id> <字段=值 ...> 或 /修正 <比赛id> 重新提取 <链接>"}]
    try:
        cid = int(parts[0])
    except ValueError:
        return [{"group_id": group_id, "text": "用法：/修正 <比赛id> <字段=值 ...>，id 应为数字"}]
    comp = db.get_competition(cid)
    if comp is None:
        return [{"group_id": group_id, "text": f"比赛 #{cid} 不存在"}]
    rest = parts[1].strip() if len(parts) > 1 else ""
    if rest.startswith("重新提取"):
        return await _amend_refetch(group_id, cid, comp, rest)
    try:
        info = _parse_amend_fields(rest)
    except ValueError as e:
        return [{"group_id": group_id, "text": f"修正参数错误：{e}\n支持：name edition official_site official_reg_start official_reg_deadline official_time tracks official_rules device_req summary（或短名 site/reg_deadline/time/rules 等）"}]
    if not info:
        return [{"group_id": group_id, "text": "请提供至少一个要修正的字段，如 /修正 1 official_reg_deadline=2026-12-01 23:59"}]
    try:
        ok = db.amend_competition(cid, info, operator=sender, note="超管 /修正")
    except ValueError as e:
        return [{"group_id": group_id, "text": f"修正失败：{e}"}]
    if not ok:
        return [{"group_id": group_id, "text": f"比赛 #{cid} 不存在，无法修正"}]
    changed = "；".join(f"{k}={v}" for k, v in info.items())
    msgs = [{"group_id": group_id, "text": f"已修正比赛「{comp['name']}」（#{cid}）：{changed}\n修正已生成版本记录"}]
    msgs += _broadcast_to_groups(comp["name"], cid, f"比赛「{comp['name']}」（#{cid}）已被超管修正：{changed}", exclude=group_id)
    return msgs


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


def cmd_pending_list(group_id: str, sender: str, args: str) -> list[dict]:
    """只在发起群展示公共比赛及字段名，不发送提取原文、操作者或个人数据。"""
    if not is_super_admin(sender):
        return [{"group_id": group_id, "text": "仅超管可查看来源待审更新"}]
    updates = db.list_pending_updates()
    if not updates:
        return [{"group_id": group_id, "text": "当前没有来源待审更新"}]
    lines = [f"来源待审更新：共 {len(updates)} 条（最多显示 20 条；编号用于 /待审批准 或 /待审拒绝）"]
    for update in updates[:20]:
        fields = update["fields"]
        keys = [field for field in _AMEND_FIELD_MAP.values() if isinstance(fields, dict) and field in fields]
        names = "、".join(dict.fromkeys(keys)) or "无有效字段（可拒绝）"
        lines.append(f"更新 #{update['id']}｜比赛 #{update['competition_id']} {update['name']}｜字段：{names}")
    return [{"group_id": group_id, "text": "\n".join(lines)}]


def cmd_pending_apply(group_id: str, sender: str, args: str) -> list[dict]:
    """超管批准部分更新；仅向发起群确认，不广播字段值。"""
    if not is_super_admin(sender):
        return [{"group_id": group_id, "text": "仅超管可批准来源待审更新"}]
    try:
        update_id = int(args.strip())
    except ValueError:
        return [{"group_id": group_id, "text": "用法：/待审批准 <更新id>（来自 /待审列表）"}]
    try:
        ok = db.apply_pending_update(update_id, operator=sender)
    except ValueError:
        return [{"group_id": group_id, "text": "待审更新无效或来源已停用，未修改正式资料；可用 /待审拒绝 处理"}]
    text = f"待审更新 #{update_id} 已批准，公共比赛资料已更新" if ok else "待审更新不存在，或比赛不是正式记录"
    return [{"group_id": group_id, "text": text}]


def cmd_pending_reject(group_id: str, sender: str, args: str) -> list[dict]:
    """超管拒绝来源更新；原因保存在本机审计，不作为群通知发送。"""
    if not is_super_admin(sender):
        return [{"group_id": group_id, "text": "仅超管可拒绝来源待审更新"}]
    parts = args.strip().split(None, 1)
    try:
        update_id = int(parts[0])
    except (ValueError, IndexError):
        return [{"group_id": group_id, "text": "用法：/待审拒绝 <更新id> [原因]（来自 /待审列表）"}]
    ok = db.reject_pending_update(update_id, operator=sender, note=parts[1] if len(parts) > 1 else "")
    text = f"待审更新 #{update_id} 已拒绝，正式资料保持原样" if ok else "待审更新不存在"
    return [{"group_id": group_id, "text": text}]


# ---------------------------------------------------------------------
# v2 命令：征集登记（替代旧活动体系）
# ---------------------------------------------------------------------

def cmd_collection_create(group_id: str, sender: str, args: str) -> list[dict]:
    """老师/超管发起征集。格式：/征集 <标题> [截止时间] [名额] [字段1,字段2] [用途=xxx]"""
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可发起征集"}]
    parts = args.strip().split(None, 1)
    if not parts:
        return [{"group_id": group_id, "text": "用法：/征集 <标题> [截止时间] [名额] [字段1,字段2]\n例如：/征集 机房设备借用 2026-11-01 20:00 5 设备,时段"}]
    title = parts[0]
    rest = parts[1].strip() if len(parts) > 1 else ""
    deadline = ""
    capacity = None
    fields: list[str] = []
    purpose = ""
    if rest:
        m = re.match(r"^(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})\b", rest)
        if m:
            deadline = f"{m.group(1)} {m.group(2)}"
            rest = rest[m.end():].strip()
        else:
            m = re.match(r"^(\d{4}-\d{2}-\d{2})\b", rest)
            if m:
                deadline = m.group(1)
                rest = rest[m.end():].strip()
        for tok in rest.split():
            if tok.startswith("用途="):
                purpose = tok.split("=", 1)[1].strip()
            elif capacity is None and tok.isdigit():
                capacity = int(tok)
            else:
                fields.extend(f.strip() for f in tok.replace("，", ",").split(",") if f.strip())
    try:
        cid = db.create_collection(group_id, title, purpose=purpose, deadline=deadline,
                                   capacity=capacity, fields=fields, operator=sender)
    except ValueError as e:
        return [{"group_id": group_id, "text": str(e)}]
    return [{"group_id": group_id, "text": f"已发起征集 #{cid}：{title}（用 /报名 {cid} ... 报名，/征集汇总 {cid} 查看）"}]


def cmd_collection_register(group_id: str, sender: str, args: str) -> list[dict]:
    """成员报名/更新/取消自己的征集登记，按人+征集幂等。"""
    parts = args.strip().split(None, 1)
    if not parts:
        return [{"group_id": group_id, "text": "用法：/报名 <征集id> [字段=值 ...]\n取消：/报名 取消 <征集id>"}]
    if parts[0] == "取消":
        rest = parts[1].strip().split(None, 1) if len(parts) > 1 else []
        if not rest:
            return [{"group_id": group_id, "text": "用法：/报名 取消 <征集id>"}]
        try:
            cid = int(rest[0])
        except ValueError:
            return [{"group_id": group_id, "text": "征集 id 应为数字"}]
        ok, msg = db.cancel_collection_registration(cid, group_id, sender)
        return [{"group_id": group_id, "text": msg}]
    try:
        cid = int(parts[0])
    except ValueError:
        return [{"group_id": group_id, "text": "征集 id 应为数字"}]
    data = None
    if len(parts) > 1:
        try:
            data = _parse_field_values(parts[1])
        except ValueError as e:
            return [{"group_id": group_id, "text": f"报名参数错误：{e}"}]
        if not data:
            return [{"group_id": group_id, "text": "请使用 字段=值 填写报名资料"}]
    ok, msg = db.register_collection(cid, group_id, sender, data)
    return [{"group_id": group_id, "text": msg}]


def cmd_collection_summary(group_id: str, sender: str, args: str) -> list[dict]:
    """群内只展示汇总；请求者是管理员也不向群成员发送个人明细。"""
    try:
        cid = int(args.strip().split()[0])
    except (ValueError, IndexError):
        return [{"group_id": group_id, "text": "用法：/征集汇总 <征集id>"}]
    coll = db.get_collection(cid)
    if coll is None or coll["group_id"] != group_id:
        return [{"group_id": group_id, "text": "征集不存在或不属于本群"}]
    s = db.summarize_collection(cid)
    remaining = "不限" if s["remaining"] is None else str(s["remaining"])
    lines = [
        f"征集：{s['title']}（{s['status']}）",
        f"用途：{s['purpose'] or '-'}",
        f"截止：{coll['deadline'] or '不限'}",
        f"已报名：{s['registered']} 人",
        f"已取消：{s['cancelled']} 人",
        f"剩余名额：{remaining}",
    ]
    return [{"group_id": group_id, "text": "\n".join(lines)}]


def cmd_collection_close(group_id: str, sender: str, args: str) -> list[dict]:
    """老师/超管关闭征集，停止报名。"""
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可关闭征集"}]
    try:
        cid = int(args.strip().split()[0])
    except (ValueError, IndexError):
        return [{"group_id": group_id, "text": "用法：/征集关闭 <征集id>"}]
    coll = db.get_collection(cid)
    if coll is None or coll["group_id"] != group_id:
        return [{"group_id": group_id, "text": "征集不存在或不属于本群"}]
    ok = db.set_collection_status(cid, "closed", sender)
    return [{"group_id": group_id, "text": "已关闭征集，停止报名" if ok else "操作失败"}]


def cmd_org_events(group_id: str, sender: str, args: str) -> list[dict]:
    """查看学校统一安排（仅共享给群的 confirmed 安排）。"""
    events = db.list_org_events(status="confirmed", shareable_only=True)
    if not events:
        return [{"group_id": group_id, "text": "当前没有已共享的学校安排"}]
    lines = ["学校统一安排："]
    for e in events:
        t = e["contest_time"] or e["reg_deadline"] or ""
        lines.append(f"#{e['id']} {e['title']}｜{t}｜{e.get('location') or '地点待定'}")
    return [{"group_id": group_id, "text": "\n".join(lines)}]


def cmd_training_list(group_id: str, sender: str, args: str) -> list[dict]:
    sessions = db.list_training_sessions(group_id)
    if not sessions:
        return [{"group_id": group_id, "text": "本群暂无课程安排"}]
    lines = ["本群课程安排（用 /课程修改 <序号> 调整）："]
    for index, s in enumerate(sessions, 1):
        need = "（需带电脑）" if s["needs_laptop"] else ""
        lines.append(
            f"{index}. #{s['id']} {s['title']}{need}｜{s['weekday']} {s['start_time']}-{s['end_time']}｜{s['location'] or '地点待定'}"
        )
    return [{"group_id": group_id, "text": "\n".join(lines)}]


_NOTICE_WINDOW_DAYS = 30
_NOTICE_TIME_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M")


def _parse_notice_time(text: str | None) -> datetime | None:
    """解析存档时间文本（与提醒模块同一套格式）；空值或解析失败返回 None。"""
    if not text:
        return None
    for fmt in _NOTICE_TIME_FORMATS:
        try:
            return datetime.strptime(str(text).strip(), fmt).replace(tzinfo=TZ)
        except ValueError:
            continue
    return None


def _is_upcoming(now: datetime, *values: str | None) -> bool:
    """判断某条安排是否属于「近期」。

    任一时间落在 now～now+30 天内即算近期（含报名中）；时间全部缺失时保留，
    因为「时间待定」的安排仍需让群成员看到；有可解析时间但全部已过期则剔除。
    """
    horizon = now + timedelta(days=_NOTICE_WINDOW_DAYS)
    parsed = [t for t in (_parse_notice_time(v) for v in values) if t is not None]
    if not parsed:
        return True
    return any(now <= t <= horizon for t in parsed)


def cmd_notice_list(group_id: str, sender: str, args: str) -> list[dict]:
    """查看本群近期通知（比赛、学校安排、课程、报名中的征集）。

    只读命令，普通成员即可使用：仅读取本群已有记录做聚合展示，
    不做权限校验之外的任何写操作，也不调用模型生成内容。
    """
    now = datetime.now(TZ)
    contests = [
        c for c in db.list_group_competitions(group_id, status="confirmed")
        if _is_upcoming(now, c.get("official_time"), c.get("official_reg_deadline"))
    ]
    org_events = [
        e for e in db.list_org_events(status="confirmed", shareable_only=True)
        if _is_upcoming(now, e.get("contest_time"), e.get("reg_deadline"))
    ]
    sessions = db.list_training_sessions(group_id)
    collections = [
        c for c in db.list_collections(group_id, status="open")
        if _is_upcoming(now, c.get("deadline"))
    ]
    if not (contests or org_events or sessions or collections):
        return [{"group_id": group_id, "text": "本群近期暂无通知。发送 /帮助 看看还能做什么。"}]

    lines = ["本群近期通知："]
    if contests:
        lines.append(f"【比赛】{len(contests)} 条")
        for c in contests:
            extra = []
            if c.get("official_time"):
                extra.append(f"比赛 {c['official_time']}")
            if c.get("official_reg_deadline"):
                extra.append(f"报名截止 {c['official_reg_deadline']}")
            if c.get("edition"):
                extra.append(c["edition"])
            lines.append("· " + (c.get("name") or "未命名比赛") + ("｜" + "｜".join(extra) if extra else ""))
    if org_events:
        lines.append(f"【学校安排】{len(org_events)} 条")
        for e in org_events:
            extra = []
            t = e.get("contest_time") or e.get("reg_deadline") or ""
            if t:
                extra.append(t)
            extra.append(e.get("location") or "地点待定")
            lines.append("· " + (e.get("title") or "未命名安排") + "｜" + "｜".join(extra))
    if sessions:
        lines.append(f"【课程安排】{len(sessions)} 条")
        for s in sessions:
            need = "（需带电脑）" if s.get("needs_laptop") else ""
            lines.append(
                "· " + f"{s.get('title') or '课程'}{need}"
                f"｜{s.get('weekday') or ''} {s.get('start_time') or ''}-{s.get('end_time') or ''}"
                f"｜{s.get('location') or '地点待定'}"
            )
    if collections:
        lines.append(f"【报名中的征集】{len(collections)} 条")
        for c in collections:
            extra = []
            if c.get("deadline"):
                extra.append(f"截止 {c['deadline']}")
            if c.get("capacity"):
                extra.append(f"名额 {c['capacity']}")
            lines.append(
                "· " + f"#{c['id']} {c.get('title') or '未命名征集'}"
                + ("｜" + "｜".join(extra) if extra else "")
                + f"｜报名回复 /报名 {c['id']}"
            )
    lines.append("详细用法：/帮助 课程｜/帮助 报名")
    return [{"group_id": group_id, "text": "\n".join(lines)}]


# 课程参数键：「/课程添加」与「/课程修改」共用同一套解析口径，
# 用正则按 `键=值` 切分，值内部允许空格（如 `时间=周四 19点半-21点`）。
_COURSE_FIELD_KEYS = ("标题", "内容", "要求", "时间", "地点", "软件", "设备", "电脑")
_COURSE_FIELD_RE = re.compile(
    r"(" + "|".join(_COURSE_FIELD_KEYS) + r")\s*[=＝]\s*(.*?)"
    r"(?=\s+(?:" + "|".join(_COURSE_FIELD_KEYS) + r")\s*[=＝]|$)",
    re.S,
)
_QUOTE_PAIRS = (('"', '"'), ("'", "'"), ("“", "”"), ("「", "」"))

_TRAINING_ADD_USAGE = (
    "用法：/课程添加 <标题> [星期几] [开始-结束] [地点]"
    "（也可写 /课程添加 图论进阶 时间=周四 19点半-21点 地点=二教B201 电脑=需要）"
)
_TRAINING_MODIFY_USAGE = (
    "用法：/课程修改 <序号或课程名> [时间=周四 19点半-21点] [内容=...] [地点=...]"
)


def _strip_quotes(value: str) -> str:
    """去掉值两侧的包裹引号（中文引号也兼容）。"""
    text = (value or "").strip()
    for left, right in _QUOTE_PAIRS:
        if len(text) >= 2 and text.startswith(left) and text.endswith(right):
            return text[1:-1].strip()
    return text


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


def cmd_training_add(group_id: str, sender: str, args: str) -> list[dict]:
    """新增课程：兼容位置参数，也支持 `时间=/地点=/电脑=` 命名参数。"""
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可添加课程"}]
    fields, rest = _split_course_fields(args)
    parts = rest.split(None, 3)
    title = fields.get("标题") or (fields.get("内容") or fields.get("要求") or "")
    if not title and parts:
        title = parts[0]
    if not title:
        return [{"group_id": group_id, "text": _TRAINING_ADD_USAGE}]
    weekday = "" if fields.get("时间") else (parts[1] if len(parts) > 1 else "")
    time_range = "" if fields.get("时间") else (parts[2] if len(parts) > 2 else "")
    location = fields.get("地点", parts[3] if len(parts) > 3 else "")
    raw_time = fields.get("时间") or " ".join(x for x in (weekday, time_range) if x)
    time_fields = _apply_time_fields(raw_time) or _fallback_time_fields(weekday, time_range)
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
    tid = db.create_training_session(group_id, info, sender)
    span = _format_course_time(info["weekday"], info["start_time"], info["end_time"])
    tail = f"（{span}）" if (info["start_time"] or info["end_time"]) and span else ""
    return [{"group_id": group_id, "text": f"已添加课程 #{tid}：{title}{tail}"}]


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


def cmd_training_modify(group_id: str, sender: str, args: str) -> list[dict]:
    """修改课程：目标支持列表序号 / #id / 课程名，字段用 `键=值` 指定。"""
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可修改课程"}]
    fields, rest = _split_course_fields(args)
    target = rest.strip()
    if not target:
        return [{"group_id": group_id, "text": _TRAINING_MODIFY_USAGE}]
    course, ambiguous = _resolve_course_target(group_id, target)
    if course is None:
        return [{"group_id": group_id, "text": ambiguous or f"找不到课程：{target}"}]
    updates: dict[str, object] = {}
    notices: list[str] = []
    if fields.get("标题"):
        updates["title"] = fields["标题"]
        if fields.get("内容"):
            notices.append("已忽略 内容=（同时给了 标题=，按标题更新）")
    elif fields.get("内容") or fields.get("要求"):
        updates["title"] = fields.get("内容") or fields.get("要求") or ""
    if fields.get("时间"):
        updates.update(_apply_time_fields(fields["时间"]))
    for key, field in (("地点", "location"), ("软件", "software"), ("设备", "device_req")):
        if fields.get(key):
            updates[field] = fields[key]
    if "电脑" in fields:
        laptop = fields["电脑"].strip()
        if laptop in ("需要", "是", "要", "1"):
            updates["needs_laptop"] = True
        elif laptop in ("不需要", "否", "不用", "0"):
            updates["needs_laptop"] = False
        else:
            notices.append("电脑= 只支持 需要/不需要")
    if not updates:
        return [{"group_id": group_id, "text": _TRAINING_MODIFY_USAGE}]
    ok = db.update_group_course(group_id, course["id"], sender, **updates)
    if not ok:
        return [{"group_id": group_id, "text": f"找不到课程：{target}"}]
    updated = db.get_group_course(group_id, course["id"]) or course
    parts: list[str] = []
    if "title" in updates:
        parts.append(f"标题={updated['title']}")
    if {"weekday", "start_time", "end_time"} & set(updates):
        span = _format_course_time(updated.get("weekday", ""), updated.get("start_time", ""),
                                   updated.get("end_time", ""))
        if span:
            parts.append(f"时间={span}")
    if "location" in updates:
        parts.append(f"地点={updated.get('location') or '地点待定'}")
    if "needs_laptop" in updates:
        parts.append(f"需带电脑={'需要' if updated.get('needs_laptop') else '不需要'}")
    if "software" in updates:
        parts.append(f"软件={updated.get('software') or '无'}")
    if "device_req" in updates:
        parts.append(f"设备={updated.get('device_req') or '无'}")
    text = f"已更新课程 #{course['id']}：" + "｜".join(parts or ["无变更"])
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


def cmd_faq_list(group_id: str, sender: str, args: str) -> list[dict]:
    # FAQ 列表含内部维护口径，仅老师/超管可见，避免普通成员看到管理端内容。
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可查看 FAQ 列表"}]
    keyword = args.strip() or None
    faqs = db.list_faq(group_id, keyword)
    if not faqs:
        return [{"group_id": group_id, "text": "本群暂无 FAQ" + ("（含关键词）" if keyword else "")}]
    lines = ["本群 FAQ："]
    for f in faqs:
        lines.append(f"#{f['id']} Q：{f['question']}")
        lines.append(f"   A：{f['answer']}")
    return [{"group_id": group_id, "text": "\n".join(lines)}]


def cmd_pub_pending_list(group_id: str, sender: str, args: str) -> list[dict]:
    """查看待超管确认的发布申请（老师看自己的，超管看全部）。"""
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可查看待确认申请"}]
    pending = db.list_pending_publications()
    if is_super_admin(sender):
        items = pending
    else:
        items = [p for p in pending if p.get("submitted_by") == sender]
    if not items:
        return [{"group_id": group_id, "text": "当前没有待确认的发布申请"}]
    lines = ["待确认的发布申请："]
    from cdutetc.alias_helper import format_user_name
    for item in items:
        lines.append(
            f"#{item['id']} {item['name'] or '未命名'}"
            f"｜{item['official_time'] or '时间待定'}"
            f"｜提交人 {format_user_name(item['submitted_by'] or '')}"
        )
        if item.get("duplicate_note"):
            lines.append(f"   ⚠ {item['duplicate_note']}")
    if is_super_admin(sender):
        lines.append("在私聊回复「同意 <编号>」或「拒绝 <编号>」处理。")
    else:
        lines.append("等待超管在私聊中确认。")
    return [{"group_id": group_id, "text": "\n".join(lines)}]


def cmd_faq_add(group_id: str, sender: str, args: str) -> list[dict]:
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可添加 FAQ"}]
    if "=" not in args:
        return [{"group_id": group_id, "text": "用法：/FAQ添加 <问题> = <答案>"}]
    q, a = args.split("=", 1)
    try:
        fid = db.add_faq(group_id, q.strip(), a.strip(), source="老师手工添加", operator=sender)
    except ValueError as e:
        return [{"group_id": group_id, "text": str(e)}]
    return [{"group_id": group_id, "text": f"已添加 FAQ #{fid}"}]


def cmd_faq_delete(group_id: str, sender: str, args: str) -> list[dict]:
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可删除 FAQ"}]
    try:
        fid = int(args.strip().split()[0])
    except (ValueError, IndexError):
        return [{"group_id": group_id, "text": "用法：/FAQ删除 <id>"}]
    ok = db.delete_faq(group_id, fid, sender)
    if not ok:
        return [{"group_id": group_id, "text": f"FAQ #{fid} 不存在或不属于本群"}]
    return [{"group_id": group_id, "text": f"已删除 FAQ #{fid}"}]


# ---------------------------------------------------------------------
# 运行时白名单（仅超管）：加群 / 加老师，改完立即生效，无需重启
# ---------------------------------------------------------------------

def _split_id_note(args: str) -> tuple[str, str]:
    parts = (args or "").strip().split(None, 1)
    if not parts:
        return "", ""
    return parts[0], parts[1].strip() if len(parts) > 1 else ""


def cmd_group_add(group_id: str, sender: str, args: str, client=None) -> list[dict]:
    if not is_super_admin(sender):
        return [{"group_id": group_id, "text": "仅超管可添加群"}]
    gid, note = _split_id_note(args)
    # 群号形态是否可用是客户端的能力，不由业务层猜字符串规则。
    if not capabilities_of(client).accepts_group_id(gid):
        return [{"group_id": group_id, "text": "官方接入不能用 QQ 群号授权。请先在目标群 @我 /帮助，"
                 "再把我给出的 /加群 指令复制到私聊。"}]
    ok, msg = roles.add_group(gid, operator=sender, note=note)
    return [{"group_id": group_id, "text": msg, "notify_super_admin": ok}]


def cmd_group_remove(group_id: str, sender: str, args: str) -> list[dict]:
    if not is_super_admin(sender):
        return [{"group_id": group_id, "text": "仅超管可移除群"}]
    gid, _ = _split_id_note(args)
    ok, msg = roles.remove_group(gid, operator=sender)
    return [{"group_id": group_id, "text": msg}]


def cmd_group_list(group_id: str, sender: str, args: str) -> list[dict]:
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可查看"}]
    return [{"group_id": group_id, "text": roles.describe()}]


def cmd_teacher_add(group_id: str, sender: str, args: str) -> list[dict]:
    if not is_super_admin(sender):
        return [{"group_id": group_id, "text": "仅超管可添加老师"}]
    uid, note = _split_id_note(args)
    ok, msg = roles.add_teacher(uid, operator=sender, note=note)
    return [{"group_id": group_id, "text": msg, "notify_super_admin": ok}]


def cmd_teacher_remove(group_id: str, sender: str, args: str) -> list[dict]:
    if not is_super_admin(sender):
        return [{"group_id": group_id, "text": "仅超管可移除老师"}]
    uid, _ = _split_id_note(args)
    ok, msg = roles.remove_teacher(uid, operator=sender)
    return [{"group_id": group_id, "text": msg}]


def cmd_teacher_list(group_id: str, sender: str, args: str) -> list[dict]:
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可查看"}]
    return [{"group_id": group_id, "text": roles.describe()}]


def cmd_claim_group(group_id: str, sender: str, args: str) -> list[dict]:
    """群内认领本群：/认领此群 <QQ群号> <群备注名>（仅超管）。

    官方协议下群号拿不回来，只能由超管在本群里把 group_openid 与真实群号
    对上号；认领后同步写入动态白名单，本群立刻具备完整功能。
    """
    from cdutetc.alias_helper import format_group_name
    if not is_super_admin(sender):
        return [{"group_id": group_id, "text": "仅超管可认领本群"}]
    if not group_id:
        return [{"group_id": group_id, "text": "请在目标群里 @我 发送该指令"}]
    parts = (args or "").strip().split(None, 1)
    if len(parts) < 2:
        return [{"group_id": group_id,
                 "text": "用法：/认领此群 <QQ群号> <群备注名>\n例：/认领此群 123456789 集训队群"}]
    numeric, name = parts[0].strip(), parts[1].strip()
    if not numeric.isdigit():
        return [{"group_id": group_id, "text": "QQ 群号应为纯数字"}]
    if not is_valid_account_id(group_id):
        return [{"group_id": group_id, "text": "当前群标识格式不对，无法认领"}]
    try:
        db.set_entity_alias("group", group_id, name, numeric, operator=sender)
    except ValueError as e:
        return [{"group_id": group_id, "text": str(e)}]
    added, detail = roles.add_group(group_id, operator=sender, note=name)
    tail = "本群已纳入自动放行名单。" if added else f"{detail}"
    return [{"group_id": group_id, "text": (
        f"✅ 已认领本群：{format_group_name(group_id)}\n{tail}"
    )}]


def cmd_claim_teacher(group_id: str, sender: str, args: str) -> list[dict]:
    """群内认领老师身份：/认领老师 <QQ号> <姓名>。

    绑定的是「发送者本人的 member_openid」，因此不需要（也无法）输入 openid；
    同时写入动态老师名单，认领后立即获得本群管理权限。
    """
    from cdutetc.alias_helper import format_user_name
    if not group_id:
        return [{"group_id": group_id, "text": "请在群里 @我 发送该指令"}]
    if not (is_super_admin(sender) or sender in config.TEACHER_WHITELIST):
        return [{"group_id": group_id,
                 "text": "仅超管或已授权老师可认领身份；新老师请让超管先 /加老师。"}]
    parts = (args or "").strip().split(None, 1)
    if len(parts) < 2:
        return [{"group_id": group_id,
                 "text": "用法：/认领老师 <QQ号> <姓名>\n例：/认领老师 10001 李老师"}]
    numeric, name = parts[0].strip(), parts[1].strip()
    if not numeric.isdigit():
        return [{"group_id": group_id, "text": "QQ 号应为纯数字"}]
    if not sender:
        return [{"group_id": group_id, "text": "取不到你的账号标识，认领失败"}]
    try:
        db.set_entity_alias("user", sender, name, numeric, operator=sender)
    except ValueError as e:
        return [{"group_id": group_id, "text": str(e)}]
    added, detail = roles.add_teacher(sender, operator=sender, note=name)
    tail = "已加入老师名单。" if added else f"{detail}"
    return [{"group_id": group_id, "text": (
        f"✅ 已认领老师身份：{format_user_name(sender)}\n{tail}\n"
        "如该群尚未登记负责老师，请超管用 /群老师 登记后再按名单收窄权限。"
    )}]


def cmd_label_group(group_id: str, sender: str, args: str) -> list[dict]:
    return _label_with_kind("group", group_id, sender, args)


def cmd_label_user(group_id: str, sender: str, args: str) -> list[dict]:
    return _label_with_kind("user", group_id, sender, args)


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


def cmd_label_list(group_id: str, sender: str, args: str) -> list[dict]:
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可查看"}]
    items = db.list_account_labels()
    if not items:
        return [{"group_id": group_id,
                 "text": "还没有命名记录。\n"
                         "用法：/命名群 <标识> <名称>、/命名人 <标识> <名称>"}]
    lines = ["已命名的标识："]
    for item in items:
        kind = "群" if item["kind"] == "group" else "人"
        aid = item["account_id"]
        short = f"{aid[:8]}…{aid[-4:]}" if len(aid) > 14 else aid
        lines.append(f"[{kind}] {item['label']}  ←  {short}")
    return [{"group_id": group_id, "text": "\n".join(lines)}]


def cmd_notify_groups(group_id: str, sender: str, args: str) -> list[dict]:
    """普通比赛按场设置范围；自动周赛的唯一配置入口是逐群周赛开关。"""
    if not is_super_admin(sender):
        return [{"group_id": group_id, "text": "仅超管可设置比赛通知群"}]
    parts = (args or "").strip().split(None, 1)
    if not parts:
        return [{"group_id": group_id,
                 "text": "用法：/通知群 <比赛id> <群号,群号>\n"
                         "      /通知群 <比赛id> 全部   （恢复通知所有允许群）\n"
                         "      /通知群 <比赛id>         （查看当前范围）"}]
    try:
        cid = int(parts[0])
    except ValueError:
        return [{"group_id": group_id, "text": "比赛 id 应为数字"}]
    comp = db.get_competition(cid)
    if comp is None:
        return [{"group_id": group_id, "text": f"比赛 #{cid} 不存在"}]
    if db.is_weekly_competition(cid):
        targets = db.get_competition_target_groups(cid)
        return [{"group_id": group_id, "text": "这是自动周赛，范围由各群周赛开关决定。\n"
                 + "当前目标：" + ("、".join(sorted(targets)) or "尚无启用群")
                 + "\n请用 /周赛设置 群名或标识 开启|关闭，其他场次的 /通知群 用法不变。"}]
    current = db.get_competition_notify_groups(cid)
    if len(parts) == 1:
        scope = "、".join(sorted(current)) if current else "全部允许群"
        return [{"group_id": group_id,
                 "text": f"比赛 #{cid}「{comp['name']}」当前通知范围：{scope}"}]
    spec = parts[1].strip()
    if spec in ("全部", "all", "0"):
        db.set_competition_notify_groups(cid, set(), operator=sender)
        return [{"group_id": group_id,
                 "text": f"比赛 #{cid} 已恢复为通知全部允许群"}]
    targets = {g.strip() for g in re.split(r"[,，\s]+", spec) if g.strip()}
    bad = [g for g in targets if not is_valid_account_id(g)]
    if bad:
        return [{"group_id": group_id, "text": f"群号格式不对：{','.join(bad)}"}]
    unknown = targets - config.ALLOWED_GROUPS
    if unknown:
        return [{"group_id": group_id,
                 "text": f"这些群未授权，先用 /加群 添加：{','.join(sorted(unknown))}"}]
    count = db.set_competition_notify_groups(cid, targets, operator=sender)
    return [{"group_id": group_id,
             "text": f"比赛 #{cid}「{comp['name']}」将只通知 {count} 个群："
                     f"{'、'.join(sorted(targets))}"}]


def cmd_ask(group_id: str, sender: str, args: str) -> list[dict]:
    """记录未答问题。若 FAQ 已命中（上层先查过），不会走到这里。"""
    question = args.strip()
    if not question:
        return [{"group_id": group_id, "text": "用法：/提问 <问题>"}]
    qid = db.add_unanswered_question(group_id, question, sender)
    return [{"group_id": group_id, "text": f"已记录问题 #{qid}，老师看到后会回答（回答后会加入 FAQ）"}]


def cmd_question_list(group_id: str, sender: str, args: str) -> list[dict]:
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可查看未答问题"}]
    items = db.list_unanswered(group_id, status="open")
    if not items:
        return [{"group_id": group_id, "text": "暂无未答问题"}]
    lines = ["未答问题："]
    for q in items:
        lines.append(f"#{q['id']} {q['question']}（提问者：{q['asker'] or '未知'}）")
    lines.append("直接回答：/回答 <编号> = <答案>")
    lines.append("（回答会自动存入本群 FAQ，之后同类问题由机器人直接答）")
    return [{"group_id": group_id, "text": "\n".join(lines)}]


_ANSWER_RE = re.compile(r"^\s*#?(\d+)\s*(?:=|＝|：|:|\s)\s*(.+)$", re.S)


def cmd_answer_question(group_id: str, sender: str, args: str) -> list[dict]:
    """老师按编号回答未答问题，并沉淀为 FAQ（一条命令完成闭环）。"""
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可回答问题"}]
    match = _ANSWER_RE.match(args or "")
    if not match:
        return [{"group_id": group_id,
                 "text": "用法：/回答 <编号> = <答案>\n例：/回答 3 = 本周日 19:00 在机房"}]
    qid = int(match.group(1))
    answer = match.group(2).strip()
    if not answer:
        return [{"group_id": group_id, "text": "答案不能为空"}]
    try:
        result = db.answer_unanswered_question(group_id, qid, answer, operator=sender)
    except ValueError as e:
        return [{"group_id": group_id, "text": str(e)}]
    if result is None:
        return [{"group_id": group_id,
                 "text": f"问题 #{qid} 不存在、不属于本群或已处理过"}]
    return [{"group_id": group_id,
             "text": f"已回答 #{qid} 并存入 FAQ #{result['faq_id']}。\n"
                     f"问题：{result['question']}\n"
                     "之后同类提问会直接由机器人回答。"}]

# ---------------------------------------------------------------------
# 阶段四：抓取与提醒状态
# ---------------------------------------------------------------------

def cmd_fetch_status(group_id: str, sender: str, args: str) -> list[dict]:
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可查看抓取状态"}]
    sources = db.list_sources()
    if not sources:
        return [{"group_id": group_id, "text": "暂无公共来源配置"}]
    lines = ["公共来源抓取状态："]
    for s in sources:
        comp = db.get_competition(s["competition_id"])
        name = comp["name"] if comp else f"#{s['competition_id']}"
        st = s["last_status"] or "未抓取"
        lines.append(f"#{s['id']} {name}｜{st}｜{s['last_fetched_at'] or '-'}｜{s['url'][:50]}")
    return [{"group_id": group_id, "text": "\n".join(lines)}]


def cmd_reminder_status(group_id: str, sender: str, args: str) -> list[dict]:
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可查看提醒状态"}]
    from cdutetc import reminder_view
    try:
        page = int(args.strip() or "1")
    except ValueError:
        return [{"group_id": group_id, "text": "用法：/提醒状态 [页码]"}]
    return [{"group_id": group_id, "text": reminder_view.render(group_id, page=page)}]


SCOPE_DENIED_GROUP = "⚠️ 该命令仅限本群老师或超级管理员执行"
SCOPE_DENIED_PRIVATE = "⛔ 私聊管理仅对超级管理员开放，请在已授权群内使用命令"
# 作用域诊断类命令：入口处按「身份 × 渠道」统一挡一层，命令内部再按作用域决定展示范围。
# 其余既有管理命令沿用各自的权限实现，避免改动已知行为。
_STATUS_COMMANDS = frozenset({"比赛状态", "群状态"})


def admin_scope(sender: str, group_id: str, *, private: bool = False) -> str | None:
    """统一作用域入口：群聊按本群，私聊按全局（仅超管）。"""
    from cdutetc import roles
    return roles.resolve_admin_scope(sender, None if private else group_id)


def cmd_contest_status(group_id: str, sender: str, args: str, *, private: bool = False) -> list[dict]:
    """比赛主动提醒诊断：私聊超管看全部已授权群，群聊只看本群。"""
    from cdutetc import reminder_preflight
    scope = admin_scope(sender, group_id, private=private)
    if scope is None:
        return [{"group_id": group_id,
                 "text": SCOPE_DENIED_PRIVATE if private else SCOPE_DENIED_GROUP}]
    if args.strip() not in ("", "全部"):
        return [{"group_id": group_id,
                 "text": "用法：/比赛状态（群内看本群；私聊超管可用 /比赛状态 全部）"}]
    from cdutetc import roles
    groups = sorted(config.ALLOWED_GROUPS) if scope == roles.SCOPE_GLOBAL else [group_id]
    return [{"group_id": group_id, "text": reminder_preflight.render_diagnosis(groups)}]


# ---------------------------------------------------------------------
# 超管私聊「目标工作群（Active Group）」会话指令
# ---------------------------------------------------------------------
# 这些指令只在私聊里生效，用于给超管私聊绑定一个目标群，之后私聊的
# /课程* /FAQ* /比赛状态 /晨报预览 等指令都按该群的单群视角执行，
# 回执全部走私聊，真实群完全无感知。
PRIVATE_SESSION_COMMANDS = frozenset({"选群", "切群", "当前群", "重置群"})


def _allowed_group_ids() -> list[str]:
    return sorted(config.ALLOWED_GROUPS)


def _group_label(group_id: str) -> str:
    from cdutetc import reminder_preflight
    return reminder_preflight.group_label(group_id)


def resolve_group_arg(arg: str) -> tuple[str | None, str]:
    """把 /选群 的参数解析成群 ID。

    支持三种写法：序号（列表里的 1/2/3…）、群备注名/命名、群号或 group_openid。
    返回 (group_id, "") 或 (None, 错误提示)。
    """
    value = (arg or "").strip()
    if not value:
        return None, "请带上目标群：/选群 <序号|群备注名|群号>"
    groups = _allowed_group_ids()
    if not groups:
        return None, "当前没有已授权群，请先用 /加群 <群号> 添加。"
    # 1) 直接命中群号/openid
    if value in config.ALLOWED_GROUPS:
        return value, ""
    # 2) 序号
    if value.isdigit():
        index = int(value)
        if 1 <= index <= len(groups):
            return groups[index - 1], ""
        return None, f"序号 {index} 超出范围（当前共 {len(groups)} 个已授权群）。"
    # 3) 群备注名 / 命名（先精确，再包含匹配，唯一命中才采纳）
    exact = [gid for gid in groups if _group_label(gid) == value]
    if len(exact) == 1:
        return exact[0], ""
    fuzzy = [gid for gid in groups if value in _group_label(gid)]
    if len(fuzzy) == 1:
        return fuzzy[0], ""
    if len(fuzzy) > 1:
        names = "、".join(f"{_group_label(gid)}({gid})" for gid in fuzzy)
        return None, f"「{value}」匹配到多个群：{names}，请改用序号或群号。"
    return None, f"没有找到群「{value}」，发送 /选群 查看全部已授权群。"


def render_group_picker(*, current: str | None = None) -> str:
    """列出全部已授权群（序号、群名/群号、是否为当前选定群）。"""
    from cdutetc.alias_helper import format_group_name
    groups = _allowed_group_ids()
    if not groups:
        return "当前没有已授权群，请先用 /加群 <群号> 添加。"
    lines = [f"📋 已授权群（{len(groups)} 个），发送 /选群 <序号> 绑定操作群："]
    for index, gid in enumerate(groups, 1):
        mark = "　✅ 当前" if gid == current else ""
        lines.append(f"{index}. {format_group_name(gid)}{mark}")
    if current:
        lines.append("发送 /重置群 可清除绑定，恢复全局管理模式。")
    else:
        lines.append("当前未绑定操作群，私聊管理指令按全局视角执行。")
    return "\n".join(lines)


def cmd_active_group(uid: str, args: str) -> list[dict]:
    """/选群、/切群：不带参数列群，带参数绑定目标群。"""
    from cdutetc import admin_session
    current = admin_session.get_active_group(uid)
    value = (args or "").strip()
    if not value:
        return [{"group_id": current or "", "text": render_group_picker(current=current)}]
    gid, error = resolve_group_arg(value)
    if gid is None:
        return [{"group_id": current or "", "text": f"⚠️ {error}"}]
    admin_session.set_active_group(uid, gid)
    text = (
        f"✅ 已将操作群切换为：【{_group_label(gid)}】(ID: {gid})\n"
        "当前私聊执行 /课程列表、/课程添加、/FAQ添加、/比赛状态 等指令将作用于该群。\n"
        "发送 /重置群 可清除绑定。"
    )
    return [{"group_id": gid, "text": text}]


def cmd_active_group_show(uid: str) -> list[dict]:
    """/当前群：查看私聊当前绑定的目标群。"""
    from cdutetc import admin_session
    gid = admin_session.get_active_group(uid)
    if not gid:
        return [{"group_id": "", "text": (
            "📍 当前未绑定操作群，私聊管理指令按全局管理模式执行。\n"
            "发送 /选群 查看已授权群并绑定目标群。"
        )}]
    if gid not in config.ALLOWED_GROUPS:
        # 白名单被运行时收回：绑定已失效，直接清掉避免误操作
        admin_session.clear_active_group(uid)
        return [{"group_id": "", "text": (
            f"⚠️ 原先绑定的群 {gid} 已不在授权列表，绑定已自动清除。\n"
            "发送 /选群 重新选择目标群。"
        )}]
    return [{"group_id": gid, "text": (
        f"📍 当前操作群：【{_group_label(gid)}】(ID: {gid})\n"
        "私聊里的 /课程*、/FAQ*、/比赛状态、/晨报预览 均作用于该群。"
    )}]


def cmd_active_group_reset(uid: str) -> list[dict]:
    """/重置群：清除绑定，私聊恢复全局管理模式。"""
    from cdutetc import admin_session
    previous = admin_session.get_active_group(uid)
    admin_session.clear_active_group(uid)
    if not previous:
        return [{"group_id": "", "text": "当前未绑定操作群，无需重置。"}]
    return [{"group_id": "", "text": (
        f"♻️ 已解除与「{_group_label(previous)}」({previous}) 的绑定。\n"
        "私聊管理指令已恢复全局管理模式。"
    )}]


def handle_session_command(cmd: str, args: str, uid: str) -> list[dict]:
    """私聊会话指令统一入口：/选群 /切群 /当前群 /重置群。"""
    if cmd in ("选群", "切群"):
        return cmd_active_group(uid, args)
    if cmd == "当前群":
        return cmd_active_group_show(uid)
    if cmd == "重置群":
        return cmd_active_group_reset(uid)
    return [{"group_id": "", "text": "没有这个命令，发送 /帮助 查看用法。"}]


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
            return [{"group_id": group_id, "text": SCOPE_DENIED_PRIVATE}]
    elif admin_scope(sender, group_id, private=False) is None:
        return [{"group_id": group_id, "text": SCOPE_DENIED_GROUP}]
    # 只读渲染，绝不调用 send_group：预览结果由调用方原路（私聊）回显
    text = daily_digest.build_daily_digest(group_id)
    header = f"{DIGEST_PREVIEW_PREFIX}目标群：{_group_label(group_id)}（{group_id}）\n"
    if not text:
        return [{"group_id": group_id, "text": header + DIGEST_PREVIEW_EMPTY}]
    return [{"group_id": group_id,
             "text": header + "以下内容仅本次预览，未发送到群：\n" + text}]


async def cmd_trigger_fetch(group_id: str, sender: str, args: str) -> list[dict]:
    """手动触发一次抓取（老师）。结果汇总后返回，不写入发送日志冒充发送。

    - 不带参数：抓取已登记的公共来源（原行为）；
    - 带「比赛」/「contest」：抓 CF / 牛客赛程，汇总新建 / 已存在 / 失败条数。
    异步执行：在现有事件循环中调度，避免嵌套 asyncio.run。
    """
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可触发抓取"}]
    keyword = (args or "").strip().lower()
    if "比赛" in keyword or "contest" in keyword:
        if not config.AUTO_CONTEST_CRAWL_ENABLED:
            return [{"group_id": group_id,
                     "text": "赛程自动抓取未开启（AUTO_CONTEST_CRAWL_ENABLED=False）"}]
        try:
            items = await crawler.run_contest_fetch()
        except Exception as e:  # noqa: BLE001 - 对外只报一句可读原因
            return [{"group_id": group_id, "text": f"赛程抓取失败：{e}"}]
        created = [i for i in items if i.get("kind") == "created"]
        exists = [i for i in items if i.get("kind") == "exists"]
        failed = [i for i in items if i.get("kind") == "error"]
        lines = [f"赛程抓取完成：新建 {len(created)} 条 / 已存在 {len(exists)} 条 / 失败 {len(failed)} 条"]
        for item in created:
            lines.append(f"+ {item.get('platform')} {item.get('name')}（{item.get('start')}，id={item.get('competition_id')}）")
        for item in failed:
            lines.append(f"! {item.get('platform') or '未知'}：{item.get('error')}")
        return [{"group_id": group_id, "text": "\n".join(lines)}]
    try:
        results = await crawler.run_fetch()
    except Exception as e:
        return [{"group_id": group_id, "text": f"抓取失败：{e}"}]
    if not results:
        return [{"group_id": group_id, "text": "暂无公共来源配置"}]
    lines = [f"本轮抓取 {len(results)} 个来源："]
    for r in results:
        if r["kind"] == "skip":
            lines.append(f"#{r['source_id']} 跳过：{r['reason']}")
        elif r["kind"] == "nochange":
            lines.append(f"#{r['source_id']} 无变化（{r['name']}）")
        elif r["kind"] == "disabled":
            lines.append("自动抓取未开启（AUTO_FETCH_ENABLED=False）")
        elif r["kind"] == "auto_created":
            lines.append(f"#{r['source_id']} 免审新建比赛：{r['name']}（id={r['competition_id']}，已 confirmed，未通知全员）")
        elif r["kind"] == "pending_auto":
            lines.append(f"#{r['source_id']} 有更新已存待审：{r['name']}（比赛 id={r['competition_id']}，更新 id={r['update_id']}，未通知全员）")
        elif r["kind"] == "updated_auto":
            lines.append(f"#{r['source_id']} 更新草稿比赛：{r['name']}（id={r['competition_id']}，未通知全员）")
        else:
            lines.append(f"#{r['source_id']} 异常：{r['error']}")
    return [{"group_id": group_id, "text": "\n".join(lines)}]


# ---------------------------------------------------------------------
# 分发器
# ---------------------------------------------------------------------

def _private_header(origin_sender: str) -> str:
    """私聊回执首行标牌：已绑定群显示工作区，否则显示全局模式。"""
    from cdutetc import admin_session
    gid = admin_session.get_active_group(origin_sender)
    if gid and gid in config.ALLOWED_GROUPS:
        label = (_group_label(gid) or "").strip()
        # _group_label 在群无备注时直接回群号，此时只显示「群 <群号>」，
        # 避免拼成「662657785 (662657785)」这类重复。
        if not label or label == gid or label == f"群 {gid}":
            label = f"群 {gid}"
        elif gid not in label:
            label = f"{label} ({gid})"
        return f"📌【当前工作区：{label}】"
    return "🌐【当前模式：全局模式 (未选群)】"


def _with_private_header(result: list[dict] | None, origin_sender: str,
                         was_private: bool) -> list[dict] | None:
    """给超管私聊的所有回执统一加首行状态标牌；群聊/普通用户原样返回。"""
    if not result or not was_private or not is_super_admin(origin_sender):
        return result
    head = _private_header(origin_sender)
    decorated: list[dict] = []
    for item in result:
        text = (item.get("text") or "").strip()
        decorated.append({**item, "text": f"{head}\n{text}"} if text else item)
    return decorated


def _bind_active_group(admin_user_id: str, token: str) -> str | None:
    """私聊发 `/群配置 <序号|群号|备注名>` 时顺手锁定工作区；解析失败静默交由主流程报错。"""
    from cdutetc import admin_session, group_admin
    try:
        gid = group_admin.resolve_group(token)
    except ValueError:
        return None
    admin_session.set_active_group(admin_user_id, gid)
    return gid


def bind_active_group(admin_user_id: str, token: str) -> str | None:
    """C5 的对外入口：私聊 `/<管理命令> <单个群标识>` 成功后自动绑定工作区。

    main 的私聊链路里，群配置类命令由 `group_admin.handle_command` 直接处理，
    不经过 `handle_command`，因此需要一个可被外部调用的绑定入口。
    """
    return _bind_active_group(admin_user_id, token)


def with_private_header(result: list[dict] | None, origin_sender: str,
                        was_private: bool = True) -> list[dict] | None:
    """C5 的对外入口：给超管私聊回执统一加工作区标牌（群聊/普通用户原样返回）。"""
    return _with_private_header(result, origin_sender, was_private)


async def handle_command(text: str, group_id: str, sender: str,
                         client=None, *, private: bool = False,
                         active_group: str | None = None) -> list[dict] | None:
    """处理一条命令消息；超管私聊回执统一带工作区状态标牌（实现见 _handle_command_impl）。"""
    return _with_private_header(
        await _handle_command_impl(text, group_id, sender, client,
                                   private=private, active_group=active_group),
        sender, private)


async def _handle_command_impl(text: str, group_id: str, sender: str,
                               client=None, *, private: bool = False,
                               active_group: str | None = None) -> list[dict] | None:
    """处理一条命令消息。

    - 非命令消息返回 None（由上层走 FAQ 匹配 / 模型问答 / 未答问题收集）。
    - 命令消息返回消息列表，每条含 group_id 与 text（text 为空表示无需发送）。
    - 命令名大小写不敏感：精确匹配失败时按 lower 兜底（兼容 /FAQ、/faq 等）。
    - 命令处理函数若为协程（如异步抓取）则在本循环内 await，避免嵌套 asyncio.run。
    - `private=True` 表示来自私聊：管理命令按全局作用域校验（仅超管），
      群聊按本群作用域校验；作用域为 None 时统一拦截，不进入任何管理逻辑。
    - `active_group` 仅对超管私聊生效：超管先 /选群 绑定了目标群后，
      私聊指令改按该群的单群视角执行（写库、查询、诊断都落到该群），
      回执仍由调用方走私聊，绝不发往真实群。
    """
    if not text.startswith("/"):
        return None
    was_private = private
    # 私聊绑定目标群时降维成「单群视角」：作用域按该群校验，展示范围也只到该群
    if private and active_group and is_super_admin(sender) and active_group in config.ALLOWED_GROUPS:
        group_id = active_group
        private = False
    cmd, args = parse_command(text)
    if cmd is None:
        return [{"group_id": group_id, "text": "请写上命令名，例如 /帮助。"}]
    # 超管私聊直接 /群配置 1 时顺手锁定工作区，下一步操作就不用再 /选群
    if was_private and is_super_admin(sender) and cmd == "群配置":
        tokens = (args or "").split()
        if len(tokens) == 1:
            _bind_active_group(sender, tokens[0])
    if cmd in ("help", "帮助"):
        return [{"group_id": group_id, "text": build_help(sender, args)}]
    scoped_cmd = cmd if cmd in _STATUS_COMMANDS else next(
        (key for key in _STATUS_COMMANDS if key.lower() == cmd.lower()), cmd)
    if scoped_cmd in _STATUS_COMMANDS and admin_scope(sender, group_id, private=private) is None:
        return [{"group_id": group_id,
                 "text": SCOPE_DENIED_PRIVATE if private else SCOPE_DENIED_GROUP}]
    from cdutetc import group_admin
    handlers = {
        "群配置": lambda gid, uid, value: group_admin.handle_command("群配置", value, uid, gid),
        "群提醒": lambda gid, uid, value: group_admin.handle_command("群提醒", value, uid, gid),
        "周赛设置": lambda gid, uid, value: group_admin.handle_command("周赛设置", value, uid, gid),
        "群老师": lambda gid, uid, value: group_admin.handle_command("群老师", value, uid, gid),
        "修正": cmd_amend,
        "待审列表": cmd_pending_list,
        "待审批准": cmd_pending_apply,
        "待审拒绝": cmd_pending_reject,
        "征集": cmd_collection_create,
        "报名": cmd_collection_register,
        "征集汇总": cmd_collection_summary,
        "征集关闭": cmd_collection_close,
        "校安排": cmd_org_events,
        "通知列表": cmd_notice_list,
        "通知": cmd_notice_list,
        "课程列表": cmd_training_list,
        "课程添加": cmd_training_add,
        "课程取消": cmd_training_cancel,
        "课程修改": cmd_training_modify,
        "FAQ列表": cmd_faq_list,
        "FAQ添加": cmd_faq_add,
        "FAQ删除": cmd_faq_delete,
        "待确认列表": cmd_pub_pending_list,
        "加群": lambda gid, uid, value: cmd_group_add(gid, uid, value, client=client),
        "删群": cmd_group_remove,
        "群列表": cmd_group_list,
        "命名群": cmd_label_group,
        "命名人": cmd_label_user,
        "命名列表": cmd_label_list,
        "认领此群": cmd_claim_group,
        "认领老师": cmd_claim_teacher,
        "加老师": cmd_teacher_add,
        "删老师": cmd_teacher_remove,
        "老师列表": cmd_teacher_list,
        "通知群": cmd_notify_groups,
        "提问": cmd_ask,
        "问题列表": cmd_question_list,
        "回答": cmd_answer_question,
        "抓取状态": cmd_fetch_status,
        "提醒状态": cmd_reminder_status,
        "比赛状态": lambda gid, uid, value: cmd_contest_status(gid, uid, value, private=private),
        "群状态": lambda gid, uid, value: cmd_contest_status(gid, uid, value, private=private),
        "晨报预览": lambda gid, uid, value: cmd_digest_preview(gid, uid, value, private=private),
        "抓取一次": cmd_trigger_fetch,
    }
    handler = handlers.get(cmd)
    if handler is None:
        lower = cmd.lower()
        for key, h in handlers.items():
            if key.lower() == lower:
                handler = h
                break
    if handler is None:
        return [{"group_id": group_id, "text": "没有这个命令，发送 /帮助 查看用法。"}]
    try:
        result = handler(group_id, sender, args)
        if asyncio.iscoroutine(result):
            result = await result
        return result
    except ValueError:
        logger.warning("命令参数校验失败 [%s]", AppErrorCode.VALIDATION.value)
        return _error_reply(group_id, AppErrorCode.VALIDATION)
    except Exception:
        logger.exception("命令处理异常 [%s]", AppErrorCode.INTERNAL.value)
        return _error_reply(group_id, AppErrorCode.INTERNAL)


async def match_faq(group_id: str, text: str, *, exact_only: bool = False) -> dict | None:
    """单次读取本群 FAQ；高置信本地匹配，模糊时按配置选择模型消歧。"""
    return await faq_search.find(group_id, text, db.list_faq(group_id), exact_only=exact_only)


async def _answer_from_knowledge_base(text: str, group_id: str) -> str | None:
    """只查库内已录入内容作答，不调用模型（普通成员问答路径）。

    FAQ 已由入口查询；此处依次尝试已录入比赛、本群课程、学校安排。
    命中返回整理好的纯事实文本；全部未命中返回 None（由调用方转人工）。

    设计要点：比赛名、时间、地点等全部原样取自数据库，不做任何推测补全，
    因此不会出现模型编造或“我没有数据库权限”这类管理端话术。
    """
    threshold = config.QA_KB_MATCH_MIN_SCORE

    # 1) 已录入比赛：Dice 相似度 + 标题关键词命中取最高（口语问法也能召回）
    best, best_score = None, 0.0
    for comp in db.list_question_contests(group_id):
        score = content_filter.record_match_score(
            text, comp.get("name") or "", comp.get("contest_time") or "",
        )
        if score > best_score:
            best, best_score = comp, score
    if best is not None and best_score >= threshold:
        name = best.get('name') or '未命名比赛'
        fields = (("报名截止", "reg_deadline"), ("比赛时间", "contest_time"),
                  ("形式", "mode"), ("地点", "location"), ("官网", "source_link"))
        wanted = set()
        if re.search(r"截止|报名.*(时间|何时|什么时候|几点|几号)", text):
            wanted.add("reg_deadline")
        if re.search(r"开赛|开打|开始|比赛时间", text) or (
            "报名" not in text and "截止" not in text and re.search(r"什么时候|几点|几号|何时", text)
        ):
            wanted.add("contest_time")
        if re.search(r"地点|在哪|哪里|哪儿|教室|机房", text):
            wanted.add("location")
        if re.search(r"形式|线上|线下", text):
            wanted.add("mode")
        if re.search(r"官网|网址|链接", text):
            wanted.add("source_link")
        if wanted:
            fields = tuple((label, key) for label, key in fields if key in wanted)
        lines = [f"「{name}」目前登记的安排："]
        for label, key in fields:
            value = (best.get(key) or "").strip()
            if value:
                lines.append(f"{label}：{value}")
            elif wanted:
                lines.append(f"{label}：还没登记，暂时无法确定")
        return "\n".join(lines)

    # 2) 本群课程安排
    best_t, best_t_score = None, 0.0
    for session in db.list_training_sessions(group_id, active_only=True):
        score = content_filter.record_match_score(
            text, session.get("title") or "", session.get("location") or "",
        )
        if score > best_t_score:
            best_t, best_t_score = session, score
    if best_t is not None and best_t_score >= threshold:
        lines = [f"【{best_t.get('title') or '集训课'}】"]
        weekday = {0: "周一", 1: "周二", 2: "周三", 3: "周四",
                   4: "周五", 5: "周六", 6: "周日"}.get(best_t.get("weekday"))
        if weekday:
            lines.append(f"时间：{weekday} {best_t.get('start_time') or ''}"
                         f"-{best_t.get('end_time') or ''}".rstrip("-"))
        if best_t.get("location"):
            lines.append(f"地点：{best_t['location']}")
        if best_t.get("needs_laptop"):
            lines.append("需要带电脑")
        if best_t.get("software"):
            lines.append(f"软件：{best_t['software']}")
        if best_t.get("device_req"):
            lines.append(f"设备：{best_t['device_req']}")
        return "\n".join(lines)

    # 3) 学校安排（仅可共享给学生的条目）
    best_e, best_e_score = None, 0.0
    for event in db.list_org_events(status="confirmed", shareable_only=True):
        score = content_filter.record_match_score(
            text, event.get("title") or "", event.get("location") or "",
        )
        if score > best_e_score:
            best_e, best_e_score = event, score
    if best_e is not None and best_e_score >= threshold:
        lines = [f"【{best_e.get('title') or '学校安排'}】"]
        for label, key in (("类型", "event_type"), ("报名截止", "reg_deadline"),
                           ("时间", "contest_time"), ("地点", "location"),
                           ("负责老师", "teacher"), ("备注", "note")):
            value = (best_e.get(key) or "").strip()
            if value:
                lines.append(f"{label}：{value}")
        return "\n".join(lines)

    return None


# 机器人能力说明：由固定话术回答，不走模型，避免模型臆造「做不到」。
# 面向群成员公开，只讲能做什么，不涉及实现细节。
CAPABILITY_TEXT = """【我能做的事】
- 查信息：比赛、课程、学校安排、常见问题都可以问我
- 自动提醒：比赛报名截止、开赛，征集截止，课程开课，按管理员设置提醒群里
- 报名：群里发起的征集，可以直接用 /报名 参加
- 发布：老师把比赛信息发给我，我整理后由超管确认，再按设置通知群里

有问题直接 @我 就行。"""


async def handle_non_command_message(text: str, group_id: str, sender: str,
                                     staff: bool = False) -> list[dict]:
    """非命令消息问答。

    闲聊本地回复，普通陈述不自动登记；非问句仅允许完整命中 FAQ 词条。
    staff=False（普通成员）：真正的队务提问只答库内资料，未命中才收集为未答问题。
    staff=True（老师/超管）：提问按 FAQ → 算法拦截 → 模型问答处理。

    text/group_id/sender 均由调用方校验完毕；本函数不做权限判断。
    """
    if content_filter.is_social_chat(text):
        if config.QA_SOCIAL_CHAT_ENABLED:
            return [{"group_id": group_id, "text": await llm.answer_social_chat(text)}]
        return []
    if re.fullmatch(r"(?:请问|查一下|看看)?(?:本群|我们群|群里|这周|本周|近期|最近|现在|今天|接下来)?"
                    r"(?:有什么|有哪些|有没有|有啥)(?:比赛|竞赛)(?:安排)?[吗呢啊呀?？。！!\s]*", text.strip()):
        from cdutetc import group_admin
        items = group_admin.contests(group_id, this_week=bool(re.search(r"这周|本周", text)))
        lines = ["本群比赛：" if items else "本群暂无符合条件的已登记比赛。"]
        lines.extend(f"· {item['name']}｜{item.get('contest_time') or '时间待定'}" for item in items[:10])
        return [{"group_id": group_id, "text": "\n".join(lines)}]
    is_request = content_filter.is_information_request(text)
    faq = await match_faq(group_id, text, exact_only=not is_request)
    if faq is not None:
        return [{"group_id": group_id, "text": faq['answer']}]
    if not is_request:
        return ([{"group_id": group_id, "text": "我在呢～有想问的直接说就好。"}]
                if config.QA_SOCIAL_CHAT_ENABLED else [])
    # 能力类提问用固定话术，避免模型臆造「我不能自动提醒」这类错误结论
    if content_filter.is_capability_query(text):
        return [{"group_id": group_id, "text": CAPABILITY_TEXT}]
    # 算法话题（题解/思路/概念科普）一律不调模型，FAQ 已优先命中，避免浪费 token
    if config.QA_PROBLEM_GATE_ENABLED and content_filter.is_algorithm_query(text):
        return [{"group_id": group_id, "text": config.QA_PROBLEM_REPLY}]

    # 普通成员：知识库优先，未命中转人工，全程不产生模型调用
    if not staff and config.QA_MEMBER_KB_ONLY:
        answer = await _answer_from_knowledge_base(text, group_id)
        if answer is not None:
            return [{"group_id": group_id, "text": answer}]
        # 可选：先用动态上下文模型作答（事实覆盖不到才转工单），默认关闭保持零 token
        if config.QA_MEMBER_CONTEXT_ANSWER and config.LLM_API_KEY:
            contextual = await _answer_from_context(text, group_id)
            if contextual is not None:
                return [{"group_id": group_id, "text": contextual}]
        qid = db.add_unanswered_question(group_id, text, sender)
        return [{"group_id": group_id,
                 "text": f"{config.QA_MEMBER_NO_MATCH_REPLY}（问题编号 #{qid}）"}]

    answer = await _answer_question_with_context(text, group_id)
    if answer.startswith("模型暂时不可用"):
        qid = db.add_unanswered_question(group_id, text, sender)
        return [{"group_id": group_id,
                 "text": f"模型暂时用不了，已先记为问题 #{qid}，老师看到后会回复。"}]
    if llm.UNKNOWN_MARKER in answer:
        qid = db.add_unanswered_question(group_id, text, sender)
        return [{"group_id": group_id,
                 "text": f"这条我暂时没查到，已记录为问题 #{qid}；"
                         "补一份 FAQ（/FAQ添加）或课程/征集后我就能直接答。"}]
    return [{"group_id": group_id, "text": answer}]


async def _answer_question_with_context(text: str, group_id: str) -> str:
    """带上当前群教务事实的模型问答；模型异常按「模型暂时不可用」返回。"""
    from cdutetc import qa_context

    group_context = qa_context.build_group_qa_context(group_id) if config.QA_CONTEXT_ENABLED else ""
    try:
        return await llm.answer_question(
            text, db.list_question_contests(group_id), group_context=group_context,
        )
    except Exception:  # noqa: BLE001 - 模型异常统一降级，不打断群聊
        return "模型暂时不可用，请稍后再试~"


async def _answer_from_context(text: str, group_id: str) -> str | None:
    """上下文模型作答；模型不可用或判定未收录时返回 None，交由调用方转工单。"""
    answer = await _answer_question_with_context(text, group_id)
    if not answer or answer.startswith("模型暂时不可用") or llm.UNKNOWN_MARKER in answer:
        return None
    return answer
