"""定时提醒模块：周期检查临近比赛/活动，发送提醒并持久化去重。

- 业务时间统一为北京时间 Asia/Shanghai（zoneinfo）。
- 阶段一比赛提醒（contests/reminders 表，兼容保留）：
  去重键 = (群, 比赛, 提醒类型, 目标时间)；比赛时间被修改后键变化，可重新提醒。
- v2 通用提醒（reminder_deliveries 表）：
  覆盖「征集报名截止」「公共比赛报名截止/开赛」「周期课程具体开课实例」，
  去重键含群、对象、提醒类型和目标时间，兼容旧冒号键；
  SQLite 原子领取与租约防止并发发送；成功回执标记 sent，失败有界重试。
- 按群发送：send_group(group_id, text) 由上层注入，保证提醒发到对象所属群。
"""

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
from config import REMIND_BEFORE_HOURS, REMIND_TYPES, TZ_NAME
from database import (
    claim_delivery,
    delivery_dedup_key,
    finish_delivery,
    has_reminded,
    list_competitions,
    list_contests,
    list_training_sessions,
    mark_reminded,
    record_delivery_success,
    reconcile_deliveries,
    training_occurrence_times,
    validate_delivery_claim,
)
from database import list_collections_all as _list_collections_all
from database import get_competition_notify_groups

logger = logging.getLogger(__name__)

TZ = ZoneInfo(TZ_NAME)
_LABELS = {"reg_deadline": "报名截止", "contest_time": "比赛开始"}
_TIME_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M")
_DELIVERY_MAX_ATTEMPTS = 3
_DELIVERY_LEASE_SECONDS = 120
_DELIVERY_SEND_TIMEOUT_SECONDS = 60


def _parse_time(text: str) -> datetime | None:
    """解析存档时间文本为带北京时区的 datetime；解析失败返回 None。"""
    if not text:
        return None
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text.strip(), fmt).replace(tzinfo=TZ)
        except ValueError:
            continue
    return None


def _due_offset_hours(delta: timedelta) -> float | None:
    """返回该剩余时间命中的「最紧」提醒节点（小时）；未命中返回 None。

    节点取自 config.REMIND_OFFSET_HOURS（降序存放）。判定方式是取所有
    「节点小时数 ≥ 剩余时间」的节点中最小的那个，也就是已进入的最紧窗口：
    剩 23 小时命中 24 小时节点，剩 0.5 小时命中 1 小时节点，剩 10 分钟
    （若配置了 0.167=10 分钟）命中 10 分钟节点。

    这样每个目标时间会按节点各发一次（去重键带节点），服务宕机错过某个
    节点后恢复时仍会按下一个更紧的节点补发，不会漏提醒；同时不需要为
    每场比赛单独创建定时器，内存占用与比赛数量无关。
    """
    if delta <= timedelta(0):
        return None
    candidates = [
        offset for offset in config.REMIND_OFFSET_HOURS
        if delta <= timedelta(hours=offset)
    ]
    return min(candidates) if candidates else None


def build_reminders(contests: list[dict], now: datetime) -> list[dict]:
    """计算当前应提醒的清单（纯函数，便于离线测试）。

    条件：目标时间在未来，且已进入 config.REMIND_OFFSET_HOURS 中某个节点窗口。
    """
    out: list[dict] = []
    for c in contests:
        for field in REMIND_TYPES:
            target = _parse_time(c.get(field))
            if target is None:
                continue
            offset = _due_offset_hours(target - now)
            if offset is None:
                continue
            out.append(
                {
                    "group_id": c.get("group_id") or "",
                    "contest_id": c["id"],
                    "name": c["name"],
                    "remind_type": field,
                    "label": _LABELS[field],
                    "target_str": c[field],
                    "offset_hours": offset,
                    "mode": c.get("mode"),
                    "location": c.get("location"),
                }
            )
    return out


def format_remind(item: dict) -> str:
    return (
        f"【提醒】{item['name']} {item['label']}时间：{item['target_str']}\n"
        f"形式：{item['mode'] or '待定'}  地点：{item['location'] or '待定'}\n"
        "请同学们做好准备，记得按时参加！"
    )


async def _send_reminder(send_group, group_id: str, text: str) -> tuple[bool, str]:
    """两代提醒共用有界发送；单条异常不阻断同轮其他提醒。"""
    try:
        ok = await asyncio.wait_for(send_group(group_id, text),
                                    timeout=_DELIVERY_SEND_TIMEOUT_SECONDS)
        return bool(ok), "" if ok else "发送失败"
    except asyncio.TimeoutError:
        return False, "发送超时"
    except Exception as error:
        logger.warning("提醒发送异常: %s", error)
        return False, str(error)


async def check_reminders(send_group) -> int:
    """扫描一次全部群记录并发送提醒。

    Args:
        send_group: 异步回调 async (group_id, text) -> bool，返回是否发送成功。

    Returns:
        本次成功发送并标记去重的条数。
    """
    now = datetime.now(TZ)
    sent = 0
    for contest in list_contests():
        group_id = contest.get("group_id") or ""
        if not group_id:
            # 旧记录未归属任何群：隔离保留，不发送提醒
            continue
        for item in build_reminders([contest], now):
            key = (group_id, item["contest_id"], item["remind_type"], item["target_str"])
            if has_reminded(*key):
                continue
            ok, error = await _send_reminder(send_group, group_id, format_remind(item))
            if ok:
                mark_reminded(*key)
                sent += 1
            else:
                logger.warning("提醒发送失败，留待下次重试: %s（%s）", item["name"], error)
    if sent:
        logger.info("本次检查成功发送 %d 条提醒", sent)
    return sent


# ---------------------------------------------------------------------
# v2 通用提醒：征集报名截止 / 公共比赛 / 群课程开课
# ---------------------------------------------------------------------

_COLLECTION_LABELS = {"deadline": "报名截止"}


def build_general_reminders(collections: list[dict], competitions: list[dict],
                            now: datetime) -> list[dict]:
    """计算通用提醒清单（纯函数，便于离线测试）。

    覆盖两类对象：
    - 征集（群级）：deadline 已进入某个提醒节点窗口 → 提醒对应群；
    - 已确认公共比赛：对每个允许群各生成一条报名截止/开赛提醒
      （比赛时间字段为空则跳过，视为待定不提醒）。

    返回条目字段：group_id / obj_type / obj_id / remind_type / target_str / text。
    """
    out: list[dict] = []
    for a in collections:
        if a.get("status") != "open" or a.get("group_id") not in config.ALLOWED_GROUPS:
            continue
        target = _parse_time(a.get("deadline") or "")
        if target is None:
            continue
        offset = _due_offset_hours(target - now)
        if offset is not None:
            out.append({
                "group_id": a["group_id"],
                "obj_type": "collection",
                "obj_id": a["id"],
                "remind_type": "deadline",
                "target_str": a["deadline"],
                "text": (
                    f"【提醒】征集「{a['title']}」报名截止：{a['deadline']}\n"
                    f"已报名同学请确认，未报名同学请抓紧（/报名 {a['id']}）"
                ),
            })
    # 公共比赛：字段是 official_ 前缀，映射到通用提醒类型
    _COMP_FIELDS = {"reg_deadline": "official_reg_deadline", "contest_time": "official_time"}
    for c in competitions:
        if c.get("status") != "confirmed":
            continue
        # 通知范围：该比赛指定了通知群就只发这些群，否则发全部允许群。
        # 这样「周赛」可以只发集训队群，不打扰新生群。
        # 纯函数离线测试可能没有数据库，取不到映射时按「全部允许群」处理。
        try:
            scoped = get_competition_notify_groups(c["id"])
        except (sqlite3.Error, OSError):
            scoped = set()
        targets = scoped & config.ALLOWED_GROUPS if scoped else set(config.ALLOWED_GROUPS)
        if not targets:
            # 指定了通知群但都不在允许列表里：宁可不提醒，也不要退回全群广播
            if scoped:
                logger.warning(
                    "比赛 #%s 的通知群 %s 均不在允许列表中，跳过提醒",
                    c["id"], ",".join(sorted(scoped)),
                )
            continue
        for kind, field in _COMP_FIELDS.items():
            target = _parse_time(c.get(field) or "")
            if target is None:
                continue
            offset = _due_offset_hours(target - now)
            if offset is not None:
                for gid in sorted(targets):
                    out.append({
                        "group_id": gid,
                        "obj_type": "competition",
                        "obj_id": c["id"],
                        "remind_type": kind,
                        "target_str": c[field],
                        "offset_hours": offset,
                        "text": (
                            f"【提醒】公共比赛「{c['name']}」{_LABELS[kind]}：{c[field]}\n"
                            f"官网：{c.get('official_site') or '待定'}\n请同学们提前做好准备！"
                        ),
                    })
    return out


def _delivery_key(item: dict) -> str:
    # 键必须包含群号：同一对象的提醒要为每个允许群各发一条，
    # 不带群号会导致多群订阅时只有第一个群收到。
    # 键还必须包含提醒节点：同一目标时间要按 24h/1h 等多个节点各发一次，
    # 不带节点会让第一个节点发完就把后续节点一起吞掉。
    base = delivery_dedup_key(item["group_id"], item["obj_type"], item["obj_id"],
                              item["remind_type"], item["target_str"])
    offset = item.get("offset_hours")
    if offset is None:
        return base
    return f"{base}|{offset:g}"


def build_training_reminders(sessions: list[dict], now: datetime) -> list[dict]:
    """按课程的具体开课日期生成群提醒；每周实例独立去重。"""
    out = []
    for session in sessions:
        group_id = session.get("group_id")
        if group_id not in config.ALLOWED_GROUPS:
            continue
        for target in training_occurrence_times(session, now, REMIND_BEFORE_HOURS):
            target_str = target.strftime("%Y-%m-%d %H:%M:%S" if target.second else "%Y-%m-%d %H:%M")
            details = []
            if session.get("needs_laptop"):
                details.append("需带电脑")
            if session.get("software"):
                details.append(f"软件：{session['software']}")
            if session.get("device_req"):
                details.append(f"设备：{session['device_req']}")
            out.append({
                "group_id": group_id, "obj_type": "training_session", "obj_id": session["id"],
                "remind_type": "start_time", "target_str": target_str,
                "text": (
                    f"【提醒】课程「{session['title']}」开始：{target_str}\n"
                    f"地点：{session.get('location') or '待定'}"
                    + ("\n" + "；".join(details) if details else "")
                    + "\n请提前做好训练准备！"
                ),
            })
    return out


async def check_general_reminders(send_group, now: datetime | None = None,
                                  max_attempts: int = _DELIVERY_MAX_ATTEMPTS) -> int:
    """复核计划、原子领取并发送；成功回执 sent，失败有界重试。

    Args:
        send_group: 异步回调 async (group_id, text) -> bool，返回是否发送成功。
        now: 当前时间（北京时区）；默认取系统当前时间。
        max_attempts: 失败重试上限，超过后标记 failed 不再自动重发。

    Returns:
        本次成功发送并标记去重的条数。
    """
    if max_attempts < 1:
        raise ValueError("提醒尝试次数必须大于零")
    fixed_now = now

    def current_time():
        current = fixed_now if fixed_now is not None else datetime.now(TZ)
        return current.replace(tzinfo=TZ) if current.tzinfo is None else current.astimezone(TZ)

    reconcile_deliveries(current_time(), max_attempts=max_attempts,
                         allowed_groups=set(config.ALLOWED_GROUPS))
    items = build_general_reminders(_list_collections_all(), list_competitions(status="confirmed"), current_time())
    # 按群取课程，不按当前是否已生效过滤；首次开课前同样需要提前提醒。
    sessions = [session for gid in sorted(config.ALLOWED_GROUPS)
                for session in list_training_sessions(gid, active_only=False)]
    items.extend(build_training_reminders(sessions, current_time()))
    sent = 0
    for item in items:
        key = _delivery_key(item)
        token = claim_delivery(
            item["group_id"], item["obj_type"], item["obj_id"], item["remind_type"], item["target_str"],
            now=current_time(), max_attempts=max_attempts, lease_seconds=_DELIVERY_LEASE_SECONDS,
            allowed_groups=set(config.ALLOWED_GROUPS),
        )
        if token is None or not validate_delivery_claim(
            key, token, now=current_time(), allowed_groups=set(config.ALLOWED_GROUPS),
        ):
            continue
        ok, error = await _send_reminder(send_group, item["group_id"], item["text"])
        # 接收到成功回执即先持久化事实，后续状态收尾抛错也不重复发送。
        try:
            receipt_saved = ok and record_delivery_success(key, token, now=current_time())
        except sqlite3.Error:
            logger.exception("消息已发送但成功事实写入失败，保留 sending 等待恢复: %s", key)
            continue
        try:
            finish_delivery(key, token, ok, error=error,
                            now=current_time(), max_attempts=max_attempts)
        except sqlite3.Error:
            logger.exception("提醒回执写入异常: %s", key)
            if receipt_saved:
                sent += 1
            continue
        if ok and receipt_saved:
            sent += 1
        elif not ok:
            logger.warning("通用提醒发送失败: %s", key)
    if sent:
        logger.info("本次通用提醒成功发送 %d 条", sent)
    return sent
