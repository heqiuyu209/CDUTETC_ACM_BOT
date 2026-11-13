"""超管按群管理及只读查询；群名/标识均来自已授权记录，不使用模型猜目标。"""

from datetime import datetime, timedelta
import re
import shlex
from zoneinfo import ZoneInfo

from cdutetc import config
from cdutetc import database as db, reminder_view
from cdutetc.utils import is_super_admin
from cdutetc.clients.base import active_protocol

COMMANDS = frozenset({"群配置", "群提醒", "周赛设置", "群老师"})
TZ = ZoneInfo(config.TZ_NAME)


def _aliases() -> dict[str, set[str]]:
    notes = {item["group_id"]: item["note"] for item in db.list_runtime_groups()}
    numeric = db.list_group_aliases()
    return {gid: {value for value in (gid, db.get_account_label("group", gid), notes.get(gid, ""),
                                     numeric.get(gid, {}).get("numeric_id", "")) if value}
            for gid in sorted(config.ALLOWED_GROUPS)}


def _name(gid: str) -> str:
    return db.get_account_label("group", gid) or next((g["note"] for g in db.list_runtime_groups()
                                                      if g["group_id"] == gid and g["note"]), gid)


def _display(gid: str) -> str:
    """展示用群名：已认领显示「备注名 (群号)」，否则退回短哈希，绝不裸露长 openid。"""
    from cdutetc.alias_helper import format_group_name
    return format_group_name(gid)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text).replace("的群", "群").removesuffix("群")


def _choices() -> str:
    groups = sorted(config.ALLOWED_GROUPS)
    if not groups:
        return "还没有授权群。先在目标群 @我 /帮助，再由超管按提示加群。"
    lines = []
    for index, gid in enumerate(groups, 1):
        lines.append(f"{index}. {_display(gid)}｜/群配置 {gid}")
    return "请选择要查询的群（也可直接发 /群配置 <序号>）：\n" + "\n".join(lines)


def _resolve_index(selector: str) -> str | None:
    """支持用候选列表里的序号选择群（/群配置 1）。"""
    text = (selector or "").strip()
    if not text.isdigit():
        return None
    groups = sorted(config.ALLOWED_GROUPS)
    index = int(text)
    return groups[index - 1] if 1 <= index <= len(groups) else None


def resolve_group(selector: str, *, question: bool = False) -> str:
    if selector in config.ALLOWED_GROUPS:
        return selector
    aliases_by_group = _aliases()
    if selector.isdigit():
        exact = [gid for gid, aliases in aliases_by_group.items() if selector in aliases]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise ValueError("群号有歧义，请使用群标识。\n" + _choices())
    by_index = _resolve_index(selector)
    if by_index:
        return by_index
    matches = []
    normalized = _normalize(selector)
    for gid, aliases in aliases_by_group.items():
        if question:
            found = any((re.search(r"(?<![A-Za-z0-9_-])" + re.escape(alias) + r"(?![A-Za-z0-9_-])", selector)
                         if alias == gid else len(_normalize(alias)) >= 2 and _normalize(alias) in normalized)
                        for alias in aliases)
        else:
            found = selector == gid or any(normalized == _normalize(alias) for alias in aliases)
        if found:
            matches.append(gid)
    if not matches and question and "群" not in selector and len(config.ALLOWED_GROUPS) == 1:
        matches = list(config.ALLOWED_GROUPS)
    if len(matches) != 1:
        raise ValueError(("群名有歧义，请使用群标识。\n" if matches else "没有匹配到唯一的已授权群。\n") + _choices())
    return matches[0]


def _teacher_names(gid: str) -> str:
    from cdutetc.alias_helper import format_user_name
    uids = db.get_group_teachers(gid)
    return "、".join(format_user_name(uid) for uid in uids) or "未登记"


def contests(gid: str, *, this_week: bool = False, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(TZ)
    start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    result = []
    for contest in db.list_question_contests(gid, limit=1000):
        dates = [value for value in (reminder_view._parse_time(contest.get(field) or "")
                                     for field in ("contest_time", "reg_deadline")) if value]
        if ((this_week and any(start <= value < start + timedelta(days=7) for value in dates))
                or (not this_week and (not dates or any(value >= now for value in dates)))):
            result.append(contest)
    return result


def describe(gid: str, *, this_week: bool = False, now: datetime | None = None, include_reminders: bool = True) -> str:
    weekly = gid in db.get_weekly_groups()
    lines = [f"【{_name(gid)}】", "周赛：" + ("已开启" if weekly else "未开启")]
    if weekly:
        lines.append(f"每周{('一','二','三','四','五','六','日')[config.WEEKLY_CONTEST_WEEKDAY]} "
                     f"{config.WEEKLY_CONTEST_START}–{config.WEEKLY_CONTEST_END}")
        if not config.WEEKLY_CONTEST_ENABLED:
            lines.append("自动生成周赛已关闭；已有比赛和提醒仍按计划执行。")
    lines.append(f"负责老师：{_teacher_names(gid)}")
    lines.append("主动提醒总开关：" + ("已启用" if config.GROUP_ACTIVE_MESSAGES_ENABLED else "已暂停"))
    lines.append("课程提醒：" + ("已开启" if db.group_course_remind_enabled(gid) else "已关闭"))
    block = db.get_group_notification_block(active_protocol(), gid)
    if block:
        lines.append(f"本群因平台权限拒绝暂停提醒 [{block['error_code']}/{block['platform_code']}]")
    items = contests(gid, this_week=this_week, now=now)
    lines.append(("本周" if this_week else "近期") + "比赛：" + ("暂无" if not items else ""))
    for item in items[:10]:
        lines.append(f"· {item['name']}｜开赛 {item.get('contest_time') or '待定'}"
                     + (f"｜报名截止 {item['reg_deadline']}" if item.get("reg_deadline") else ""))
    if len(items) > 10:
        lines.append(f"共{len(items)}场，此处显示前10场。")
    if include_reminders:
        plans = reminder_view.build_preview(gid, now=now)
        lines.append(f"未来7天主动提醒：{len(plans)}项")
        for plan in plans[:6]:
            lines.append(f"· {plan['scheduled_at']:%m-%d %H:%M} {plan['name']}｜{plan['label']}｜{plan['state']}")
        if plans:
            lines.append(f"完整提醒：/群提醒 {gid}")
    if not db.get_group_teachers(gid):
        lines.append(f"登记负责老师：/群老师 {gid} 老师标识（先用 /老师列表 查看已授权老师）")
    return "\n".join(lines)


def _teacher_id(selector: str) -> str:
    allowed = set(config.TEACHER_WHITELIST) | set(config.SUPER_ADMIN)
    if selector in allowed:
        return selector
    found = [uid for uid in allowed if db.get_account_label("user", uid) == selector]
    if len(found) != 1:
        raise ValueError("老师名未登记或有歧义，请使用 /老师列表 中的完整标识；未授权老师需先 /加老师。")
    return found[0]


def _describe_all(*, this_week: bool = False) -> str:
    lines = ["各群配置："]
    for gid in sorted(config.ALLOWED_GROUPS):
        names = "、".join(item["name"] for item in contests(gid, this_week=this_week)[:3]) or "暂无比赛"
        lines.append(f"· {_name(gid)}｜周赛{'开' if gid in db.get_weekly_groups() else '关'}"
                     f"｜负责老师 {_teacher_names(gid)}｜{names}")
    return "\n".join(lines) if config.ALLOWED_GROUPS else _choices()


def _parse_course_remind(parts: list[str], current_group: str) -> tuple[str, bool]:
    """解析「/群配置 [群名或序号] 课程提醒 开启|关闭」，返回 (群号, 是否开启)。"""
    if "课程提醒" not in parts:
        raise ValueError("用法：/群配置 [群名或序号] 课程提醒 开启|关闭")
    index = parts.index("课程提醒")
    target = parts[:index]
    action = parts[index + 1:]
    if len(target) > 1 or len(action) != 1 or action[0] not in ("开启", "关闭"):
        raise ValueError("用法：/群配置 [群名或序号] 课程提醒 开启|关闭")
    if target:
        gid = resolve_group(target[0])
    elif current_group:
        gid = current_group
    else:
        raise ValueError("请指定群：/群配置 <序号> 课程提醒 开启|关闭")
    return gid, action[0] == "开启"


def handle_command(command: str, args: str, sender: str, current_group: str = "") -> list[dict] | None:
    if command not in COMMANDS:
        return None
    reply = lambda text, **extra: [{"group_id": current_group, "text": text, **extra}]
    # 作用域闸门：私聊只有超管（global），群聊是超管或本群老师（group）；
    # 群内查询永远只能落到 current_group，跨群与配置类操作仍锁在超管。
    from cdutetc import roles
    scope = roles.resolve_admin_scope(sender, current_group or None)
    if scope is None:
        return reply("⚠️ 该命令仅限本群老师或超级管理员执行" if current_group
                     else "⛔ 私聊管理仅对超级管理员开放，请在已授权群内使用命令")
    if command in ("群配置", "周赛设置", "群老师") and not is_super_admin(sender):
        return reply("群配置与变更仅超管执行；本群提醒可用 /群提醒、/比赛状态 查看。")
    try:
        parts = shlex.split(args)
        if not parts:
            if not current_group:
                return reply(_choices())
            parts = [current_group]
        if command == "群配置" and parts == ["全部"]:
            if current_group:
                return reply("跨群信息请在私聊中查询。")
            return reply(_describe_all())
        if command == "群配置" and "课程提醒" in parts:
            remind_gid, enabled = _parse_course_remind(parts, current_group)
            if current_group and remind_gid != current_group:
                return reply("跨群信息请在私聊中查询。")
            db.set_group_course_remind(remind_gid, enabled, sender)
            return reply(f"{_name(remind_gid)}课程提醒已{'开启' if enabled else '关闭'}。")
        try:
            gid = resolve_group(parts[0])
        except ValueError:
            if current_group:
                return reply("请在私聊中选择其他群；查看本群可用 /群配置。")
            raise
        if current_group and gid != current_group:
            return reply("跨群信息请在私聊中查询。")
        if command == "群配置":
            if len(parts) != 1:
                raise ValueError("用法：/群配置 群名或标识；私聊可用 /群配置 全部")
            return reply(describe(gid))
        if command == "群提醒":
            if len(parts) > 2:
                raise ValueError("用法：/群提醒 群名或标识 [页码]")
            try:
                page = int(parts[1]) if len(parts) == 2 else 1
            except ValueError:
                raise ValueError("用法：/群提醒 群名或标识 [页码]") from None
            return reply(reminder_view.render(gid, page=page))
        if command == "周赛设置":
            if len(parts) != 2 or parts[1] not in ("开启", "关闭"):
                raise ValueError("用法：/周赛设置 群名或标识 开启|关闭")
            db.set_weekly_group_enabled(gid, parts[1] == "开启", operator=sender)
            message = f"{_name(gid)}的周赛已{parts[1]}，其他群设置不变。"
            if not config.WEEKLY_CONTEST_ENABLED:
                message += "自动生成周赛已关闭；群设置已保存，已有场次按当前范围执行。"
            return reply(message, refresh_weekly=True)
        if len(parts) == 1:
            return reply(f"{_name(gid)}负责老师：{_teacher_names(gid)}\n设置：/群老师 {gid} 老师标识,老师标识；清空：/群老师 {gid} 清空")
        selectors = re.split(r"[,，\s]+", " ".join(parts[1:]))
        teacher_ids = set() if selectors == ["清空"] else {_teacher_id(value) for value in selectors}
        db.set_group_teachers(gid, teacher_ids, operator=sender)
        return reply(f"{_name(gid)}负责老师：{_teacher_names(gid)}")
    except ValueError as error:
        return reply(str(error))


def answer_private(text: str, sender: str) -> str:
    """仅由已校验超管身份的私聊入口调用，全部自然语言请求均只读。"""
    if not is_super_admin(sender):
        return "私聊按群查询仅向超管开放，请在群内 @我 提问。"
    has_action = re.search(r"开启|关闭|添加|删除|设置|改成|修改|清空", text)
    is_query = re.search(r"是否|有没有|有没|开关|状态|吗[?？。！!\s]*$|[?？]", text)
    explicit_action = re.search(r"(?:帮我|替我|麻烦|请|^把)(?:把.+?)?(?:开启|关闭|添加|删除|设置|修改|清空)", text)
    if has_action and (not is_query or explicit_action):
        return "配置变更请用明确命令：/周赛设置 群名 开启|关闭，或 /群老师 群名 老师标识。"
    if not re.search(r"比赛|周赛|老师|群|提醒|配置|课程", text):
        return "我在～可以问“这周集训队群有什么比赛，有哪些老师”，或用 /群配置 群名 查看。"
    if re.search(r"每个群|各个群|所有群|各群", text):
        return _describe_all(this_week=bool(re.search(r"这周|本周|这个星期", text)))
    try:
        gid = resolve_group(text, question=True)
    except ValueError as error:
        return str(error)
    if "提醒" in text and not re.search(r"比赛|老师|配置|课程", text):
        return reminder_view.render(gid)
    return describe(gid, this_week=bool(re.search(r"这周|本周|这个星期", text)))
