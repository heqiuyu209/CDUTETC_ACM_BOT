"""管理命令：群白名单、教师授权、标注与别名、群开关、meme、工作区与帮助面板。"""

import logging
import re
from datetime import datetime
from cdutetc import config, roles, database as db
from cdutetc.clients.base import capabilities_of
from cdutetc.errors import AppErrorCode
from cdutetc.utils import can_manage_group, is_super_admin, is_valid_account_id

from cdutetc.handlers import _shared as _shared

logger = logging.getLogger(__name__)


def _role_level(member_id: str | None) -> int:
    if is_super_admin(member_id):
        return _shared._HELP_LEVEL_SUPER
    if can_manage_group(member_id):
        return _shared._HELP_LEVEL_TEACHER
    return _shared._HELP_LEVEL_MEMBER


def build_help(member_id: str | None, topic: str = "", *, private: bool = False) -> str:
    """默认给简短入口，指定主题时再显示对应身份的用法。

    - 普通队员只看到群成员/老师可用的命令，超管命令一律不下发；
    - `private=True`（超管私聊）时额外在默认页顶部给出超管快捷命令区。
    """
    level = _role_level(member_id)
    topic = topic.strip()
    topic = _shared._HELP_TOPIC_ALIASES.get(topic, topic)
    if not topic:
        lines = [_shared._HELP_HEADER,
                 "查比赛、问事情：在群里 @我 + 你的问题",
                 "看课程：/课程列表",
                 "报名：/报名 编号",
                 "详细用法：",
                 "/帮助 比赛｜/帮助 课程｜/帮助 报名｜/帮助 问答"]
        if level >= _shared._HELP_LEVEL_TEACHER:
            lines.extend(("提醒检查：/帮助 提醒", "群和老师管理：/帮助 管理"))
        if level >= _shared._HELP_LEVEL_SUPER:
            if private:
                lines.extend(_shared._HELP_SUPER_QUICK)
            else:
                lines.append("超管命令：私聊发 /帮助 查看快捷命令")
        return "\n".join(lines)
    if topic != "全部" and topic not in _shared._HELP_TOPICS:
        return "帮助主题：比赛、课程、报名、问答；老师/超管还可看 提醒、管理，超管可看 修正。\n例如：/帮助 比赛"
    selected = _shared._HELP_TOPICS.get(topic)
    lines = [f"【{topic}用法】"]
    for title, rows in _shared._HELP_SECTIONS:
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
    lines.extend(example for need, example in _shared._HELP_EXAMPLES.get(topic, ()) if need <= level)
    if level >= _shared._HELP_LEVEL_SUPER and topic in ("管理", "全部"):
        lines.append(_shared._HELP_SUPER_FOOTER)
    lines.append("返回：/帮助；完整命令：/帮助 全部")
    return "\n".join(lines)


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
    _, msg = roles.remove_group(gid, operator=sender)
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
    _, msg = roles.remove_teacher(uid, operator=sender)
    return [{"group_id": group_id, "text": msg}]


def cmd_teacher_list(group_id: str, sender: str, args: str) -> list[dict]:
    if not can_manage_group(sender):
        return [{"group_id": group_id, "text": "仅老师/超管可查看"}]
    return [{"group_id": group_id, "text": roles.describe()}]


def cmd_label_group(group_id: str, sender: str, args: str) -> list[dict]:
    return _shared._label_with_kind("group", group_id, sender, args)


def cmd_label_user(group_id: str, sender: str, args: str) -> list[dict]:
    return _shared._label_with_kind("user", group_id, sender, args)


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


def admin_scope(sender: str, group_id: str, *, private: bool = False) -> str | None:
    """统一作用域入口：群聊按本群，私聊按全局（仅超管）。"""
    from cdutetc import roles
    return roles.resolve_admin_scope(sender, None if private else group_id)


# ---------------------------------------------------------------------
# 超管私聊「目标工作群（Active Group）」会话指令
# ---------------------------------------------------------------------
# 这些指令只在私聊里生效，用于给超管私聊绑定一个目标群，之后私聊的
# /课程* /FAQ* /比赛状态 /晨报预览 等指令都按该群的单群视角执行，
# 回执全部走私聊，真实群完全无感知。
PRIVATE_SESSION_COMMANDS = frozenset({"选群", "切群", "当前群", "重置群", "切换", "管理", "面板"})


# 省略群标识时自动落到「超管当前工作区」的群级管理命令：
# 这些命令不必再粘贴 32 位 group_openid，也不必先 /选群。
# 由 group_admin.handle_command 自行解析目标群的命令：它们支持
# 「32 位 openid / 序号 / 群名关键字」三种写法，省略时落到超管工作区。
GROUP_ADMIN_COMMANDS = frozenset({"群配置", "群提醒", "周赛设置", "群老师", "绑老师"})


WORKSPACE_COMMANDS = frozenset({
    "群配置", "群提醒", "周赛设置", "群老师", "绑老师", "群开关", "偷图", "删图",
})


# 常用短别名，等价于原命令（旧写法保持可用）。
COMMAND_ALIASES = {
    "改": "修正",
    "绑老师": "群老师",
    "面板": "管理",
    "待确认": "待确认列表",
}


def canonical_command(cmd: str) -> str:
    """把短别名（/改、/绑老师、/面板、/待确认）归一成正式命令名；未命中原样返回。"""
    if cmd in COMMAND_ALIASES:
        return COMMAND_ALIASES[cmd]
    lower = cmd.lower()
    for alias, target in COMMAND_ALIASES.items():
        if alias.lower() == lower:
            return target
    return cmd


def resolve_group_arg(arg: str) -> tuple[str | None, str]:
    """把 /选群 的参数解析成群 ID。

    支持三种写法：序号（列表里的 1/2/3…）、群备注名/命名、群号或 group_openid。
    返回 (group_id, "") 或 (None, 错误提示)。
    """
    value = (arg or "").strip()
    if not value:
        return None, "请带上目标群：/选群 <序号|群备注名|群号>"
    groups = _shared._allowed_group_ids()
    if not groups:
        return None, "当前没有已授权群，请先用 /加群 <群号> 添加。"
    # 1) 直接命中群号/openid
    if value in config.ALLOWED_GROUPS:
        return value, ""
    # 2) 已认领的真实群号，可能被重复绑定，必须唯一匹配。
    if value.isdigit():
        aliases = db.list_group_aliases()
        numeric_matches = [gid for gid in groups if value == aliases.get(gid, {}).get("numeric_id")]
        if len(numeric_matches) == 1:
            return numeric_matches[0], ""
        if len(numeric_matches) > 1:
            return None, f"群号 {value} 对应多个已授权群，请改用序号或群标识。"
        # 3) 序号
        index = int(value)
        if 1 <= index <= len(groups):
            return groups[index - 1], ""
        return None, f"序号 {index} 超出范围（当前共 {len(groups)} 个已授权群）。"
    # 4) 群备注名 / 命名（先精确，再包含匹配，唯一命中才采纳）
    exact = [gid for gid in groups if _shared._group_label(gid) == value]
    if len(exact) == 1:
        return exact[0], ""
    fuzzy = [gid for gid in groups if value in _shared._group_label(gid)]
    if len(fuzzy) == 1:
        return fuzzy[0], ""
    if len(fuzzy) > 1:
        names = "、".join(f"{_shared._group_label(gid)}({gid})" for gid in fuzzy)
        return None, f"「{value}」匹配到多个群：{names}，请改用序号或群号。"
    return None, f"没有找到群「{value}」，发送 /选群 查看全部已授权群。"


def render_group_picker(*, current: str | None = None) -> str:
    """列出全部已授权群（序号、群名/群号、是否为当前选定群）。"""
    from cdutetc.alias_helper import format_group_name
    groups = _shared._allowed_group_ids()
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
        f"✅ 已将操作群切换为：【{_shared._group_label(gid)}】(ID: {gid})\n"
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
        f"📍 当前操作群：【{_shared._group_label(gid)}】(ID: {gid})\n"
        "私聊里的 /课程*、/FAQ*、/比赛状态、/晨报预览 均作用于该群。"
    )}]


def cmd_active_group_reset(uid: str) -> list[dict]:
    """/重置群：清除绑定，私聊恢复全局管理模式（同时落库显式清空标记）。"""
    from cdutetc import admin_session
    previous = admin_session.get_active_group(uid)
    # 即使内存里没有绑定，也要落一条显式清空记录：否则「自动优选的默认工作区」
    # 会让横幅又回到「📌 当前工作区」，用户的重置意图看起来没生效。
    admin_session.clear_active_group(uid)
    if not previous:
        return [{"group_id": "", "text": (
            "♻️ 已取消工作区自动优选，私聊管理指令恢复全局管理模式。\n"
            "重新锁定：/切换 <序号|群名>"
        )}]
    return [{"group_id": "", "text": (
        f"♻️ 已解除与「{_shared._group_label(previous)}」({previous}) 的绑定。\n"
        "私聊管理指令已恢复全局管理模式。"
    )}]


def cmd_switch_workspace(uid: str, args: str) -> list[dict]:
    """/切换：按序号或群名关键字切换工作区并落库（等价 /群配置 <序号|群名>）。"""
    from cdutetc import group_admin
    value = (args or "").strip()
    if not value:
        return [{"group_id": "", "text": group_admin.choices()}]
    try:
        gid, message = group_admin.switch_workspace(uid, value)
    except ValueError as error:
        return [{"group_id": "", "text": f"⚠️ {error}"}]
    return [{"group_id": gid, "text": f"✅ {message}"}]


def render_admin_panel(uid: str, gid: str, now: datetime | None = None) -> str:
    """一屏总览：工作区、待确认发布、未来 24 小时提醒、短命令速查。"""
    from cdutetc import admin_session, reminder_view
    now = now or datetime.now(_shared.TZ)
    locked = admin_session.get_active_group(uid)
    if not gid:
        return "\n".join([
            "🌐 工作区：全局模式（未锁定任何群）",
            "发送 /切换 <序号|群名> 绑定工作区，之后管理命令就不必再带群标识。",
        ])
    label = _shared._group_label(gid)
    state = "已锁定" if locked == gid else "默认"
    lines = [f"📌 工作区：{label}（{state}）"]
    try:
        collect_state = "开启" if db.meme_collect_enabled(gid) else "关闭"
    except Exception:  # noqa: BLE001 - 面板不能因为开关读取异常整体失败
        collect_state = "未知"
        logger.warning("管理面板收图开关读取失败 [%s]", AppErrorCode.INTERNAL.value)
    lines.append(f"🖼 自动收图：{collect_state}（/偷图 开|关 切换）")
    pending = db.list_pending_publications(status="pending")
    if pending:
        ids = "、".join(f"#{item['id']}" for item in pending[:5])
        more = "…" if len(pending) > 5 else ""
        lines.append(f"🗂 待确认发布：{len(pending)} 条（{ids}{more}）→ /待确认列表")
    else:
        lines.append("🗂 待确认发布：无")
    try:
        upcoming = reminder_view.build_preview(gid, now=now, days=1)
    except Exception:  # 面板不能因为提醒预览异常而整体失败
        logger.warning("管理面板提醒预览失败 [%s]", AppErrorCode.INTERNAL.value)
        upcoming = []
    if upcoming:
        lines.append(f"⏰ 未来 24 小时提醒（{len(upcoming)} 条）：")
        for item in upcoming[:5]:
            lines.append(f"   {_shared._panel_time(item['scheduled_at'])} {item['name']}·{item['label']}")
        if len(upcoming) > 5:
            lines.append(f"   …另 {len(upcoming) - 5} 条，详见 /群提醒")
    else:
        lines.append("⏰ 未来 24 小时提醒：无")
    lines.append("⌨️ 速查：/切换 集训队｜/绑老师 老师姓名｜/改 <id> 11-29 09:00｜/群提醒")
    return "\n".join(lines)


def cmd_admin_panel(group_id: str, sender: str, args: str, *, gid: str = "") -> list[dict]:
    """/管理、/面板：超管一屏总览；群里只展示本群。"""
    if not is_super_admin(sender):
        return [{"group_id": group_id, "text": "仅超管可查看管理面板 [permission]",
                 "error_code": AppErrorCode.PERMISSION.value}]
    return [{"group_id": group_id, "text": render_admin_panel(sender, gid or group_id)}]


def handle_session_command(cmd: str, args: str, uid: str) -> list[dict]:
    """私聊会话指令统一入口：/选群 /切群 /切换 /当前群 /重置群 /管理。"""
    if cmd in ("选群", "切群"):
        return cmd_active_group(uid, args)
    if cmd == "切换":
        return cmd_switch_workspace(uid, args)
    if cmd == "当前群":
        return cmd_active_group_show(uid)
    if cmd == "重置群":
        return cmd_active_group_reset(uid)
    if cmd in ("管理", "面板"):
        from cdutetc import group_admin
        return cmd_admin_panel("", uid, args, gid=group_admin.active_workspace(uid))
    return [{"group_id": "", "text": "没有这个命令，发送 /帮助 查看用法。"}]


def _private_header(origin_sender: str) -> str:
    """私聊回执首行标牌：解析出有效工作区就显示工作区，否则显示全局模式。

    工作区口径与命令执行一致（复用 `group_admin.active_workspace`）：
    已锁定 > 重启后从库里恢复的绑定 > 自动优选的正式群；
    只有超管主动 `/重置群` 过、且库里留有显式清空标记时才回到全局模式。
    """
    from cdutetc import group_admin
    try:
        gid = group_admin.active_workspace(origin_sender)
    except Exception:
        gid = ""
    if gid and gid in config.ALLOWED_GROUPS:
        alias_name, numeric = _shared._group_alias_parts(gid)
        label = alias_name or (_shared._group_label(gid) or "").strip()
        # _group_label 在群无备注时直接回群号，此时不算「有名称」，
        # 避免拼成「123456789 (123456789)」这类重复。
        if label in (gid, f"群 {gid}"):
            label = ""
        number = numeric or ""
        if label and number and number not in label:
            label = f"{label} ({number})"
        elif not label:
            # 优先用认领时留下的真实群号，没有才退回 openid。
            label = f"群 {number or gid}"
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
        if item.get("active_group"):
            decorated.append(item)
            continue
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


GROUP_SWITCH_ALIASES = {
    "问答": "qa_enabled",
    "问答开关": "qa_enabled",
    "比赛提醒": "contest_remind_enabled",
    "比赛": "contest_remind_enabled",
    "课程提醒": "course_remind_enabled",
    "课程": "course_remind_enabled",
    "晨报": "daily_digest_enabled",
    "每日晨报": "daily_digest_enabled",
}


GROUP_SWITCH_LABELS = {
    "qa_enabled": "问答",
    "contest_remind_enabled": "比赛提醒",
    "course_remind_enabled": "课程提醒",
    "daily_digest_enabled": "晨报",
}


_SWITCH_OFF_HINTS = {
    "qa_enabled": "关闭后本群不再自动回答提问，仍可发送 /命令 查询。",
    "contest_remind_enabled": "关闭后本群不再收到比赛报名/开赛提醒。",
    "course_remind_enabled": "关闭后本群不再收到课程开课提醒。",
    "daily_digest_enabled": "关闭后本群不再收到每日晨报。",
}


def render_group_switches(group_id: str) -> str:
    """渲染本群开关面板：未设置过的开关按默认开启展示。"""
    states = db.get_group_switches(group_id)
    lines = ["本群主动能力开关："]
    for key in ("qa_enabled", "contest_remind_enabled", "course_remind_enabled",
                "daily_digest_enabled"):
        enabled = states.get(key, True)
        lines.append(f"- {GROUP_SWITCH_LABELS[key]}：{'开启' if enabled else '关闭'}")
    lines.append("调整：/群开关 <问答|比赛提醒|课程提醒|晨报> 开启|关闭")
    return "\n".join(lines)


def cmd_group_switch(group_id: str, sender: str, args: str) -> list[dict]:
    """群内细粒度开关：无参数看面板，带名称与取值则改本群单项设置。"""
    if admin_scope(sender, group_id) is None:
        return [{"group_id": group_id, "text": "⚠️ 群开关仅限本群老师或超级管理员调整。"}]
    import shlex

    parts = shlex.split(args or "")
    if not parts:
        return [{"group_id": group_id, "text": render_group_switches(group_id)}]
    if len(parts) != 2:
        raise ValueError("用法：/群开关 <问答|比赛提醒|课程提醒|晨报> 开启|关闭")
    key = GROUP_SWITCH_ALIASES.get(parts[0])
    if key is None:
        raise ValueError("开关名称只支持：问答 / 比赛提醒 / 课程提醒 / 晨报")
    if parts[1] not in ("开启", "关闭"):
        raise ValueError("取值只支持 开启 或 关闭")
    enabled = parts[1] == "开启"
    db.set_group_switch(group_id, key, enabled, sender)
    text = f"本群{GROUP_SWITCH_LABELS[key]}已{parts[1]}。"
    if not enabled:
        text += _SWITCH_OFF_HINTS[key]
    return [{"group_id": group_id, "text": text}]


MEME_TOGGLE_ON = frozenset({"开", "开启", "打开", "启用", "on", "1", "是"})


MEME_TOGGLE_OFF = frozenset({"关", "关闭", "关掉", "停用", "off", "0", "否"})


def cmd_meme_collect(group_id: str, sender: str, args: str, *,
                     private: bool = False) -> list[dict]:
    """/偷图：按群开关零审核自动收图（群内管理，或超管私聊针对工作区）。"""
    if admin_scope(sender, group_id, private=private) is None:
        return [{"group_id": group_id,
                 "text": "⚠️ 自动收图开关仅限本群老师或超级管理员调整。"}]
    if not str(group_id or "").strip():
        return [{"group_id": group_id, "text": "请先 /切换 绑定一个工作区群，再执行 /偷图。"}]
    value = (args or "").strip().lower()
    if not value:
        state = "开启" if db.meme_collect_enabled(group_id) else "关闭"
        return [{"group_id": group_id,
                 "text": f"本群自动收图：{state}。用法：/偷图 开 或 /偷图 关"}]
    if value in MEME_TOGGLE_ON:
        enabled = True
    elif value in MEME_TOGGLE_OFF:
        enabled = False
    else:
        raise ValueError("用法：/偷图 开 或 /偷图 关")
    db.set_meme_collect_enabled(group_id, enabled, sender)
    if not enabled:
        return [{"group_id": group_id, "text": "已关闭本群自动收图：之后群友发的图不再收录。"}]
    total = 0
    try:
        total = db.count_meme_hashes()
    except Exception:  # noqa: BLE001 - 图库计数只用于提示，读不到不算失败
        logger.warning("收图库计数失败 [%s]", AppErrorCode.INTERNAL.value)
    return [{"group_id": group_id,
             "text": f"✅ 本群自动收图已开启（图库现有 {total} 张）："
                     "群友发的表情包会自动过滤截图/大图、去重后归入对应分类。"}]


def cmd_meme_delete(group_id: str, sender: str, args: str, *,
                    private: bool = False) -> list[dict]:
    """/删图：删掉本群最近一次机器人发出的表情包，并把 MD5 拉黑防复收。"""
    if admin_scope(sender, group_id, private=private) is None:
        return [{"group_id": group_id,
                 "text": "⚠️ 删图仅限本群老师或超级管理员执行。"}]
    if not str(group_id or "").strip():
        return [{"group_id": group_id, "text": "请先 /切换 绑定一个工作区群，再执行 /删图。"}]
    from cdutetc import memes
    outcome = memes.delete_last_sent(group_id)
    if not outcome:
        return [{"group_id": group_id, "text": "本群还没有机器人发图记录，没有可删的图。"}]
    relative = outcome.get("relative_path") or ""
    if outcome.get("md5"):
        text = f"🗑 已删除并拉黑：{relative}（同一张图以后不会再被收录）"
    else:
        text = f"🗑 已删除：{relative}（内置素材，不在收图台账中）"
    return [{"group_id": group_id, "text": text}]
