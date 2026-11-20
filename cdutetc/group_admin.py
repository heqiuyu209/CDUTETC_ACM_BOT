"""超管按群管理及只读查询；群名/标识均来自已授权记录，不使用模型猜目标。"""

import re
import shlex
from contextlib import closing
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from cdutetc import config, reminder_view
from cdutetc import database as db
from cdutetc.clients.base import active_protocol
from cdutetc.utils import is_super_admin

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
        lines.append(f"{index}. {_display(gid)}｜/切换 {index}")
    return "请选择要操作的群（也可直接发 /切换 <序号|群名关键字>）：\n" + "\n".join(lines)


# 默认工作区：没有历史绑定时优先落到「正式群」而不是测试群。
# 名称命中这些词的群一律视为调试用群，不会被当作默认工作区。
_TEST_GROUP_HINTS = ("测试", "test", "demo", "调试", "沙盒", "sandbox", "临时")


def _safe_name(gid: str) -> str:
    try:
        return _name(gid) or ""
    except Exception:
        return ""


def _is_test_group(gid: str) -> bool:
    low = _safe_name(gid).lower()
    return any(word in low for word in _TEST_GROUP_HINTS)


def default_workspace() -> str:
    """默认工作区：名称含「集训队」且非测试群优先，其次任意非测试群，最后兜底第一个。"""
    groups = sorted(config.ALLOWED_GROUPS)
    if not groups:
        return ""
    formal = [gid for gid in groups if not _is_test_group(gid)]
    preferred = [gid for gid in formal if "集训队" in _safe_name(gid)]
    return (preferred or formal or groups)[0]


def active_workspace(sender: str = "", *, fallback: bool = True) -> str:
    """超管当前工作区。

    - 已锁定：直接返回（含重启后从数据库恢复的绑定）；
    - 从未选定：回落到默认正式群（fallback=True 时），避免挂到测试群；
    - 显式 /重置群 过：保持全局模式，不再自动回落。
    """
    from cdutetc import admin_session

    gid = admin_session.get_active_group(sender) if sender else None
    if gid and gid in config.ALLOWED_GROUPS:
        return gid
    if not fallback or not sender:
        return ""
    try:
        if db.has_admin_workspace(sender):
            return ""
    except Exception:
        return ""
    return default_workspace()


def choices() -> str:
    """对外暴露的选择提示（/切换、/群配置 等无参数时展示）。"""
    return _choices()


def switch_workspace(sender: str, selector: str) -> tuple[str, str]:
    """把超管私聊工作区切到 selector 指定的群并立即落库（等价 /群配置 <序号|群名>）。"""
    from cdutetc import admin_session

    text = (selector or "").strip()
    if not text:
        raise ValueError(_choices())
    gid = resolve_group(text)
    admin_session.set_active_group(sender, gid)
    return gid, (f"已将操作群切换为 {_display(gid)}；后续私聊管理命令默认作用于该群。"
                 "\n看总览：/管理｜解除锁定：/重置群")



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


_SORT_TAIL = datetime.max.replace(tzinfo=TZ)


def _contest_sort_key(contest: dict) -> tuple:
    """「近期比赛」统一排序键：开赛时间正序 → 同时间 id 升序 → 时间未定排最后。"""
    kickoff = reminder_view._parse_time(contest.get("contest_time") or "")
    cid = contest.get("id")
    return (1 if kickoff is None else 0, kickoff or _SORT_TAIL,
            cid if isinstance(cid, int) else 10 ** 9)


def contests(gid: str, *, this_week: bool = False, now: datetime | None = None) -> list[dict]:
    """本群「近期比赛」列表：所有入口（/群配置 面板、/近期比赛 等）共用同一套时间正序。

    列表严格按开赛时间由近到远排列，时间未定的排最后，同时间按 id 升序；
    面板与命令侧只负责截取前 10 条，因此截断后保留的必然是最靠前的比赛。
    今日已开赛但仍在进行中的比赛同样保留，不会被「开赛时间已过」过滤掉。
    """
    now = now or datetime.now(TZ)
    start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    result = []
    for contest in db.list_question_contests(gid, limit=1000, order="start"):
        kickoff = reminder_view._parse_time(contest.get("contest_time") or "")
        dates = [value for value in (kickoff, reminder_view._parse_time(contest.get("reg_deadline") or ""))
                 if value]
        ongoing_today = kickoff is not None and kickoff.date() == now.date()
        if this_week:
            if any(start <= value < start + timedelta(days=7) for value in dates):
                result.append(contest)
        elif not dates or any(value >= now for value in dates) or ongoing_today:
            result.append(contest)
    result.sort(key=_contest_sort_key)
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
            lines.append("完整提醒：/群提醒（默认当前工作区）")
    if not db.get_group_teachers(gid):
        lines.append("登记负责老师：/绑老师 老师标识（先用 /老师列表 查看已授权老师）")
    return "\n".join(lines)


def _teacher_id(selector: str) -> str:
    allowed = set(config.TEACHER_WHITELIST) | set(config.SUPER_ADMIN)
    if selector.isdigit():
        found = {item["openid"] for item in db.list_entity_aliases("user")
                 if item["numeric_id"] == selector}
        if len(found) > 1:
            raise ValueError("这个 QQ 号对应多个老师标识，请使用 /老师列表 中的完整标识。")
        if not found:
            raise ValueError(f"QQ {selector} 尚未认领为老师身份。请使用 /老师列表 中的真实老师标识："
                             " /绑老师 <老师标识> <姓名>；"
                             "已授权老师可在群内 @我 /认领老师 <QQ号> <姓名> 后再按 QQ 绑定。")
        uid = next(iter(found))
        if uid not in allowed:
            raise ValueError("该 QQ 对应的老师尚未授权，请先用真实老师标识 /加老师，再登记负责群。")
        return uid
    if selector in allowed:
        return selector
    found = [uid for uid in allowed if db.get_account_label("user", uid) == selector]
    if len(found) != 1:
        raise ValueError("老师名未登记或有歧义，请使用 /老师列表 中的完整标识；未授权老师需先 /加老师。")
    return found[0]


def _teacher_target(parts: list[str], *, workspace: str, show_choices: bool) -> tuple[str, list[str]]:
    """老师的 QQ 不是群选择器；仍优先识别显式群并拒绝群/老师撞号。"""
    if parts and parts[0].isdigit():
        selector = parts[0]
        try:
            gid = resolve_group(selector)
        except ValueError as error:
            if "歧义" in str(error):
                raise
            if workspace:
                return workspace, parts
            raise ValueError("请先 /切换 <序号|群名> 选定工作区，再 /绑老师 <QQ号或老师标识> [姓名]。") from None
        if any(item["numeric_id"] == selector for item in db.list_entity_aliases("user")):
            raise ValueError("首个数字同时对应群和老师 QQ，请明确群名或群标识："
                             " /绑老师 <群名或群标识> <QQ号或老师标识> [姓名]。")
        return gid, parts[1:]
    return _target_group(parts, workspace=workspace, show_choices=show_choices)


def _teacher_binding(rest: list[str]) -> tuple[set[str], tuple[str, str] | None]:
    """兼容老师列表；标识/QQ 后的一项姓名只描述已解析的同一个老师。"""
    if rest == ["清空"]:
        return set(), None
    if len(rest) == 2 and not any(re.search(r"[,，]", part) for part in rest):
        first, name = rest
        allowed = set(config.TEACHER_WHITELIST) | set(config.SUPER_ADMIN)
        if first.isdigit() or first in allowed:
            uid = _teacher_id(first)
            is_identity = (name in allowed or name.isdigit() or re.fullmatch(r"[A-Za-z0-9_-]{24,}", name)
                           or any(db.get_account_label("user", teacher) == name for teacher in allowed))
            if is_identity:
                other = _teacher_id(name)
                return {uid, other}, None
            else:
                if not name.strip() or len(name) > 40:
                    raise ValueError("老师姓名需为 1–40 字；多个老师请用逗号分隔。")
                return {uid}, (uid, name)
    selectors = re.split(r"[,，\s]+", " ".join(rest))
    return {_teacher_id(value) for value in selectors}, None


def _save_teacher_binding(gid: str, teachers: set[str], named: tuple[str, str] | None, operator: str) -> None:
    if named is None:
        db.set_group_teachers(gid, teachers, operator=operator)
        return
    # 负责群与可选姓名一起提交，避免绑定失败仍覆盖已认领姓名。
    with closing(db._connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if gid not in config.ALLOWED_GROUPS:
            raise ValueError("群尚未授权")
        if not teachers.issubset(set(config.TEACHER_WHITELIST) | set(config.SUPER_ADMIN)):
            raise ValueError("包含未授权的老师，请先 /加老师，再登记负责群；本操作不会授予老师权限")
        uid, name = named
        stamp = db.now_str()
        conn.execute("INSERT INTO account_labels(kind,account_id,label,updated_at) VALUES('user',?,?,?)"
                     " ON CONFLICT(kind,account_id) DO UPDATE SET label=excluded.label,updated_at=excluded.updated_at",
                     (uid, name, stamp))
        conn.execute("UPDATE entity_aliases SET display_name=?,updated_at=? WHERE entity_type='user' AND openid=?",
                     (name, stamp, uid))
        db.add_audit(operator, "", "set_label", "user", uid, "", name, conn=conn)
        conn.execute("DELETE FROM group_teacher_assignments WHERE group_id=?", (gid,))
        conn.executemany("INSERT INTO group_teacher_assignments(group_id,user_id,updated_by,updated_at) VALUES(?,?,?,?)",
                         [(gid, teacher, operator, stamp) for teacher in sorted(teachers)])
        db.add_audit(operator, gid, "set_group_teachers", "group", gid, "", ",".join(sorted(teachers)), conn=conn)


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


def _target_group(parts: list[str], *, workspace: str,
                  show_choices: bool = True) -> tuple[str, list[str]]:
    """解析命令的目标群与剩余参数，统一支持 openid / 序号 / 群名关键字。

    省略群标识时默认使用超管当前工作区，从而免除拷贝 32 位 group_openid：
    ``/绑老师 孔老师``、``/群提醒``、``/群配置`` 都直接作用于工作区。
    """
    if not parts:
        if workspace:
            return workspace, []
        raise ValueError(_choices())
    try:
        return resolve_group(parts[0]), parts[1:]
    except ValueError as error:
        # 群内不给候选列表，避免把其他群名暴露给本群成员
        choices = f"\n{_choices()}" if show_choices else ""
        # 歧义（多个群同名）必须显式消歧，不能悄悄落到工作区
        if "歧义" in str(error):
            raise ValueError(f"{error}{choices}") from None
        # 首个 token 不像群标识（如 /绑老师 孔老师、/周赛设置 开启）时，
        # 整段都当作参数，作用群取超管工作区，实现免 group_openid 的短命令。
        if workspace and not _looks_like_group_ref(parts[0]):
            return workspace, parts
        if workspace:
            raise ValueError(f"没有匹配到唯一的已授权群。{choices}") from None
        raise


def _looks_like_group_ref(token: str) -> bool:
    """粗略判断 token 是否在表达「群」：纯数字序号、含群字的名称、或群 openid。"""
    text = (token or "").strip()
    if not text:
        return False
    if text.isdigit():
        return True
    return "群" in text or text in config.ALLOWED_GROUPS


def handle_command(command: str, args: str, sender: str, current_group: str = "",
                   *, private: bool = False) -> list[dict] | None:
    if command not in COMMANDS:
        return None
    def reply(text: str, **extra) -> list[dict]:
        return [{"group_id": current_group, "text": text, **extra}]

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
        # 群内始终锁本群；私聊取超管工作区（重启后按库恢复，未选定则回落正式群）。
        # 私聊不再硬锁群：显式带上群标识时仍允许跨群操作，省略时才落到工作区。
        if private:
            workspace, locked = active_workspace(sender), ""
        else:
            workspace, locked = current_group, current_group
        if command == "群配置" and parts == ["全部"]:
            if locked:
                return reply("跨群信息请在私聊中查询。")
            return reply(_describe_all())
        if command == "群配置" and "课程提醒" in parts:
            remind_gid, enabled = _parse_course_remind(parts, workspace)
            if locked and remind_gid != locked:
                return reply("跨群信息请在私聊中查询。")
            db.set_group_course_remind(remind_gid, enabled, sender)
            return reply(f"{_name(remind_gid)}课程提醒已{'开启' if enabled else '关闭'}。")
        target = _teacher_target if command == "群老师" else _target_group
        gid, rest = target(parts, workspace=workspace, show_choices=not locked)
        if locked and gid != locked:
            return reply("跨群信息请在私聊中查询。")
        if command == "群配置":
            if rest:
                raise ValueError("用法：/群配置 [群名或序号]；私聊可用 /群配置 全部")
            return reply(describe(gid))
        if command == "群提醒":
            if len(rest) > 1:
                raise ValueError("用法：/群提醒 [群名或序号] [页码]")
            try:
                page = int(rest[0]) if rest else 1
            except ValueError:
                raise ValueError("用法：/群提醒 [群名或序号] [页码]") from None
            return reply(reminder_view.render(gid, page=page))
        if command == "周赛设置":
            if len(rest) != 1 or rest[0] not in ("开启", "关闭"):
                raise ValueError("用法：/周赛设置 [群名或序号] 开启|关闭")
            db.set_weekly_group_enabled(gid, rest[0] == "开启", operator=sender)
            message = f"{_name(gid)}的周赛已{rest[0]}，其他群设置不变。"
            if not config.WEEKLY_CONTEST_ENABLED:
                message += "自动生成周赛已关闭；群设置已保存，已有场次按当前范围执行。"
            return reply(message, refresh_weekly=True)
        if not rest:
            return reply(f"{_name(gid)}负责老师：{_teacher_names(gid)}"
                         "\n设置：/绑老师 <QQ号或老师标识> [姓名]；多个老师用逗号分隔；清空：/绑老师 清空")
        teacher_ids, named = _teacher_binding(rest)
        _save_teacher_binding(gid, teacher_ids, named, sender)
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
