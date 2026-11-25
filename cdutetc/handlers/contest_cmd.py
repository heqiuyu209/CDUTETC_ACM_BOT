"""比赛命令：/比赛修改、待审发布与待应用更新、通知列表、抓取状态与触发、归属认领。"""

import logging
from datetime import datetime
from cdutetc import config, crawler, roles, database as db
from cdutetc.errors import AppErrorCode
from cdutetc.utils import can_manage_group, is_super_admin, is_valid_account_id

from cdutetc.handlers import _shared as _shared
from cdutetc.handlers import admin_cmd as _admin_cmd

logger = logging.getLogger(__name__)


def _parse_amend_fields(text: str) -> dict[str, str]:
    return _shared._parse_field_values(text, _shared._AMEND_FIELD_MAP)


def _parse_inline_time(text: str, now: datetime | None = None) -> str | None:
    """把「11-29 09:00」「2026-11-29 09:00」「11月29日 9:00」补全为完整时间。"""
    match = _shared._AMEND_INLINE_TIME.match((text or "").strip())
    if match is None:
        return None
    now = now or datetime.now(_shared.TZ)
    year = int(match["year"]) if match["year"] else now.year
    try:
        moment = datetime(year, int(match["month"]), int(match["day"]),
                          int(match["hour"]), int(match["minute"]))
    except ValueError:
        return None
    return moment.strftime("%Y-%m-%d %H:%M")


def _smart_amend_fields(rest: str) -> tuple[dict[str, str], str]:
    """解析修正参数：保留旧式 `字段=值`，同时支持免引号的单值快捷写法。

    返回值：(字段字典, 识别说明)；无法识别时抛 ValueError。
    """
    text = (rest or "").strip()
    if not text:
        raise ValueError("请提供要改的值，如 /改 12 11-29 09:00")
    if "=" in text:
        return _parse_amend_fields(text), ""
    when = _parse_inline_time(text)
    if when:
        return {"official_time": when}, f"开赛时间→{when}"
    if _shared._AMEND_URL_PATTERN.match(text):
        return {"official_site": text}, "官网链接"
    if _shared._looks_like_tracks(text):
        return {"tracks": text}, "赛道"
    return {"name": text}, "比赛名称"


async def cmd_amend(group_id: str, sender: str, args: str) -> list[dict]:
    """超管修正已发布比赛：字段纠正或重新提取官网，生成修正版本并通知各群。

    用法：
      /修正 <比赛id> <字段=值 ...>      纠正指定字段（旧写法，保持兼容）
      /改 <比赛id> <值>                 免引号快捷写法：
                                        · 11-29 09:00 / 2026-11-29 09:00 → 改开赛时间
                                        · http(s)://…                    → 改官网
                                        · C++、Python                    → 改赛道
                                        · 其他文本                       → 改比赛名称
      /修正 <比赛id> 重新提取 <链接>    重新抓取官网并提取，覆盖官方字段
    """
    if not is_super_admin(sender):
        return [{"group_id": group_id, "text": "仅超管可修正比赛信息 [permission]", "error_code": AppErrorCode.PERMISSION.value}]
    parts = args.strip().split(None, 1)
    if not parts:
        return [{"group_id": group_id, "text": "用法：/改 <比赛id> <值>（时间/链接/名称/赛道），或 /修正 <比赛id> 字段=值"}]
    try:
        cid = int(parts[0])
    except ValueError:
        return [{"group_id": group_id, "text": "用法：/改 <比赛id> <值>，id 应为数字"}]
    comp = db.get_competition(cid)
    if comp is None:
        return [{"group_id": group_id, "text": f"比赛 #{cid} 不存在"}]
    rest = parts[1].strip() if len(parts) > 1 else ""
    if rest.startswith("重新提取"):
        return await _shared._amend_refetch(group_id, cid, comp, rest)
    try:
        info, hint = _smart_amend_fields(rest)
    except ValueError as e:
        return [{"group_id": group_id, "text": f"修正参数错误：{e}\n支持：name edition official_site official_reg_start official_reg_deadline official_time tracks official_rules device_req summary（或短名 site/reg_deadline/time/rules 等）"}]
    if not info:
        return [{"group_id": group_id, "text": "请提供至少一个要修正的字段，如 /改 1 11-29 09:00"}]
    try:
        ok = db.amend_competition(cid, info, operator=sender, note="超管 /修正")
    except ValueError as e:
        return [{"group_id": group_id, "text": f"修正失败：{e}"}]
    if not ok:
        return [{"group_id": group_id, "text": f"比赛 #{cid} 不存在，无法修正"}]
    changed = "；".join(f"{k}={v}" for k, v in info.items())
    tail = "修正已生成版本记录"
    if "official_time" in info:
        # amend_competition 已按新开赛时间重排「开赛前 N 分钟」提醒队列
        tail += "；开赛提醒已按新时间重排"
    elif hint:
        tail += f"（识别为{hint}）"
    msgs = [{"group_id": group_id, "text": f"已修正比赛「{comp['name']}」（#{cid}）：{changed}\n{tail}"}]
    msgs += _shared._broadcast_to_groups(comp["name"], cid, f"比赛「{comp['name']}」（#{cid}）已被超管修正：{changed}", exclude=group_id)
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
        keys = [field for field in _shared._AMEND_FIELD_MAP.values() if isinstance(fields, dict) and field in fields]
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


def _parse_notice_time(text: str | None) -> datetime | None:
    """解析存档时间文本（与提醒模块同一套格式）；空值或解析失败返回 None。"""
    if not text:
        return None
    for fmt in _shared._NOTICE_TIME_FORMATS:
        try:
            return datetime.strptime(str(text).strip(), fmt).replace(tzinfo=_shared.TZ)
        except ValueError:
            continue
    return None


def cmd_notice_list(group_id: str, sender: str, args: str) -> list[dict]:
    """查看本群近期通知（比赛、学校安排、课程、报名中的征集）。

    只读命令，普通成员即可使用：仅读取本群已有记录做聚合展示，
    不做权限校验之外的任何写操作，也不调用模型生成内容。
    """
    now = datetime.now(_shared.TZ)
    contests = [
        c for c in db.list_group_competitions(group_id, status="confirmed")
        if _shared._is_upcoming(now, c.get("official_time"), c.get("official_reg_deadline"))
    ]
    org_events = [
        e for e in db.list_org_events(status="confirmed", shareable_only=True)
        if _shared._is_upcoming(now, e.get("contest_time"), e.get("reg_deadline"))
    ]
    sessions = db.list_training_sessions(group_id)
    collections = [
        c for c in db.list_collections(group_id, status="open")
        if _shared._is_upcoming(now, c.get("deadline"))
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
            extra.append((e.get("location") or "").strip() or "待定")
            lines.append("· " + (e.get("title") or "未命名安排") + "｜" + "｜".join(extra))
    if sessions:
        lines.append(f"【课程安排】{len(sessions)} 条")
        for s in sessions:
            need = "（需带电脑）" if s.get("needs_laptop") else ""
            lines.append(
                "· " + f"{s.get('title') or '课程'}{need}"
                f"｜{s.get('weekday') or ''} {s.get('start_time') or ''}-{s.get('end_time') or ''}"
                f"｜{(s.get('location') or '').strip() or '待定'}"
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
    # 提交人展示与待确认卡片共用同一套实时身份解析，避免两处口径不一致
    try:
        from cdutetc.main import _format_submitter
    except ImportError:  # 单模块加载等极端场景降级为旧渲染
        _format_submitter = format_user_name
    for item in items:
        lines.append(
            f"#{item['id']} {item['name'] or '未命名'}"
            f"｜{item['official_time'] or '时间待定'}"
            f"｜提交人 {_format_submitter(item['submitted_by'] or '')}"
        )
        if item.get("duplicate_note"):
            lines.append(f"   ⚠ {item['duplicate_note']}")
    if is_super_admin(sender):
        lines.append("在私聊回复「同意 <编号>」或「拒绝 <编号>」处理。")
    else:
        lines.append("等待超管在私聊中确认。")
    return [{"group_id": group_id, "text": "\n".join(lines)}]


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
    """群内老师报号：/认领老师 <QQ号> <姓名>。

    绑定的是「发送者本人的 member_openid」，因此不需要（也无法）输入 openid。
    官方协议事件里没有 QQ 号，这条命令是 QQ→openid 映射的唯一数据来源：
    报号只建立映射，不自动授予老师权限，授权统一由超管 /加老师 <QQ号> 完成。
    """
    from cdutetc.alias_helper import format_user_name
    if not group_id:
        return [{"group_id": group_id, "text": "请在群里 @我 发送该指令"}]
    # 超管身份由配置决定，认领老师会把超管 openid 写进老师表，
    # 让后续发布人解析把超管当老师展示（身份串号），因此直接在入口拦下。
    if is_super_admin(sender):
        return [{"group_id": group_id, "text": (
            "⚠️ 当前账号为超级管理员，请让老师本人用自己的 QQ 在群内发送 "
            "/认领老师 <QQ号> <姓名> 完成报号，避免覆盖超管身份"
        )}]
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
    # 报号只建立 QQ→openid 映射，不自动授权；授权权独占于超管 /加老师，
    # 防止任意成员报号即冒充老师获得管理权限。
    return [{"group_id": group_id, "text": (
        f"✅ 已登记老师报号：{format_user_name(sender)}（QQ: {numeric}）\n"
        f"身份暂未生效，请让超管发送 /加老师 {numeric} [备注] 完成授权。"
    )}]


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


def cmd_contest_status(group_id: str, sender: str, args: str, *, private: bool = False) -> list[dict]:
    """比赛主动提醒诊断：私聊超管看全部已授权群，群聊只看本群。"""
    from cdutetc import reminder_preflight
    scope = _admin_cmd.admin_scope(sender, group_id, private=private)
    if scope is None:
        return [{"group_id": group_id,
                 "text": _shared.SCOPE_DENIED_PRIVATE if private else _shared.SCOPE_DENIED_GROUP}]
    if args.strip() not in ("", "全部"):
        return [{"group_id": group_id,
                 "text": "用法：/比赛状态（群内看本群；私聊超管可用 /比赛状态 全部）"}]
    from cdutetc import roles
    groups = sorted(config.ALLOWED_GROUPS) if scope == roles.SCOPE_GLOBAL else [group_id]
    return [{"group_id": group_id, "text": reminder_preflight.render_diagnosis(groups)}]


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
        except Exception as e:
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
