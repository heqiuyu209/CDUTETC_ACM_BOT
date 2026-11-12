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
from math import ceil
from zoneinfo import ZoneInfo

from cdutetc import config
from cdutetc.clients.base import active_capabilities, active_protocol
from cdutetc.config import TZ_NAME
from cdutetc.database import (
    claim_delivery,
    delivery_dedup_key,
    finish_delivery,
    list_competitions,
    list_training_sessions,
    record_delivery_success,
    reconcile_deliveries,
    training_occurrence_times,
    validate_delivery_claim,
)
from cdutetc.database import list_collections_all as _list_collections_all
from cdutetc.database import get_competition_target_groups
from cdutetc.database import get_group_notification_block
from cdutetc.database import group_course_remind_enabled

logger = logging.getLogger(__name__)

TZ = ZoneInfo(TZ_NAME)
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


def _due_offset_hours(delta: timedelta, offsets: tuple[float, ...] | None = None) -> float | None:
    """返回该剩余时间命中的「最紧」提醒节点（小时）；未命中返回 None。

    节点默认取自 config.REMIND_OFFSET_HOURS（降序存放），可用 ``offsets``
    传入自定义节点集（课程提醒会在此之上追加分钟级节点）。判定方式是取所有
    「节点小时数 ≥ 剩余时间」的节点中最小的那个，也就是已进入的最紧窗口：
    剩 23 小时命中 24 小时节点，剩 0.5 小时命中 1 小时节点，剩 10 分钟
    （若配置了 0.167=10 分钟）命中 10 分钟节点。

    这样每个目标时间会按节点各发一次（去重键带节点），服务宕机错过某个
    节点后恢复时仍会按下一个更紧的节点补发，不会漏提醒；同时不需要为
    每场比赛单独创建定时器，内存占用与比赛数量无关。
    """
    if delta <= timedelta(0):
        return None
    nodes = config.REMIND_OFFSET_HOURS if offsets is None else tuple(offsets)
    candidates = [
        offset for offset in nodes
        if delta <= timedelta(hours=offset)
    ]
    return min(candidates) if candidates else None


def training_offset_hours() -> tuple[float, ...]:
    """课程提醒节点：既有小时级节点 + 配置的分钟级额外节点（降序去重）。"""
    extra = tuple(minutes / 60 for minutes in config.TRAINING_REMIND_EXTRA_OFFSETS_MINUTES)
    return tuple(sorted(set(config.REMIND_OFFSET_HOURS) | set(extra), reverse=True))


def _countdown(target: datetime, now: datetime) -> str:
    """按发送时刻计算剩余时间，避免延迟投递仍照抄节点分钟数。"""
    seconds = (target - now).total_seconds()
    if seconds < 60:
        return "不到1分钟"
    minutes = ceil(seconds / 60)
    if minutes < 60:
        return f"约{minutes}分钟"
    hours, remainder = divmod(minutes, 60)
    return f"约{hours}小时" + (f"{remainder}分钟" if remainder else "")


def _platform_paused(group_id: str) -> bool:
    """该群是否被平台暂停主动推送（仅存在主动权限约束的接入才可能有记录）。"""
    if not active_capabilities().supports_passive_reply:
        return False
    return get_group_notification_block(active_protocol(), group_id) is not None


async def check_contest_start_reminders(send_group, now: datetime | None = None,
                                       max_attempts: int = _DELIVERY_MAX_ATTEMPTS) -> int:
    """投递事务创建的开赛前30/15分钟计划；不动态补建逾期节点。"""
    from cdutetc.database import list_due_contest_start_deliveries

    if max_attempts < 1:
        raise ValueError("提醒尝试次数必须大于零")

    def clock():
        value = now if now is not None else datetime.now(TZ)
        return value.replace(tzinfo=TZ) if value.tzinfo is None else value.astimezone(TZ)

    current = clock()
    allowed = set(config.ALLOWED_GROUPS)
    reconcile_deliveries(current, max_attempts=max_attempts, allowed_groups=allowed)
    comps = {item["id"]: item for item in list_competitions(status="confirmed")}
    sent = 0
    for delivery in list_due_contest_start_deliveries(current, allowed):
        if _platform_paused(delivery["group_id"]):
            continue
        current = clock()
        competition = comps.get(delivery["obj_id"])
        if not competition:
            continue
        scheduled = _parse_time(delivery.get("scheduled_at") or "")
        expires = _parse_time(delivery.get("expires_at") or "")
        if scheduled is None or expires is None or not scheduled <= current <= expires:
            continue
        key = delivery["dedup_key"]
        token = claim_delivery(
            delivery["group_id"], "competition", delivery["obj_id"], "contest_start",
            delivery["target_time"], now=current, max_attempts=max_attempts,
            lease_seconds=_DELIVERY_LEASE_SECONDS, allowed_groups=allowed,
            offset_hours=_contest_offset_hours(delivery),
        )
        if token is None or not validate_delivery_claim(key, token, now=clock(), allowed_groups=allowed):
            continue
        target = _parse_time(delivery["target_time"])
        text = (
            f"【开赛提醒】「{competition['name']}」还有{_countdown(target, clock())}就开始啦。\n"
            f"开赛时间：{delivery['target_time']}\n"
            + (f"官网：{competition['official_site']}\n" if competition.get('official_site') else "")
            + "记得提前登录，检查好比赛环境。"
        )
        result = await _send_reminder_result(send_group, delivery["group_id"], text)
        if not result[0]:
            logger.warning("比赛 #%s 开赛提醒未发送 [%s]", delivery["obj_id"], result[2])
        try:
            receipt_saved = result[0] and record_delivery_success(key, token, now=current)
            finish_delivery(key, token, result[0], error=result[1], error_code=result[2],
                            now=current, max_attempts=max_attempts)
        except sqlite3.Error:
            logger.exception("开赛提醒回执写入异常: %s", key)
            continue
        if result[0] and receipt_saved:
            sent += 1
    return sent


def _contest_offset_minutes(delivery: dict) -> int:
    try:
        return round(float(delivery["dedup_key"].rsplit("|", 1)[1]) * 60)
    except (IndexError, TypeError, ValueError):
        return 30


def _contest_offset_hours(delivery: dict) -> float:
    return _contest_offset_minutes(delivery) / 60


async def _send_reminder_result(send_group, group_id: str, text: str) -> tuple[bool, str, str]:
    """发送失败保留适配器提供的稳定错误码，旧 bool 回调保持兼容。"""
    try:
        result = await asyncio.wait_for(send_group(group_id, text), timeout=_DELIVERY_SEND_TIMEOUT_SECONDS)
        if hasattr(result, "ok"):
            ok = bool(result.ok)
            code = getattr(result, "code", "")
            return ok, "" if ok else str(getattr(result, "user_hint", "") or ""), str(getattr(code, "value", code) or "")
        ok = bool(result)
        return ok, "" if ok else "[unknown] 发送结果未确认", "" if ok else "unknown"
    except asyncio.TimeoutError:
        return False, "[timeout_unknown] 发送等待超时，平台是否已接受无法确认", "timeout_unknown"
    except Exception as error:
        logger.warning("提醒发送异常 [%s]", type(error).__name__)
        return False, "[unknown] 发送结果无法确认，请检查平台连接", "unknown"


# ---------------------------------------------------------------------
# v2 通用提醒：征集报名截止 / 公共比赛 / 群课程开课
# ---------------------------------------------------------------------

def build_general_reminders(collections: list[dict], competitions: list[dict],
                            now: datetime) -> list[dict]:
    """结合当前群范围计算通用提醒清单。

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
                "offset_hours": offset,
                "text": (
                    f"【报名提醒】「{a['title']}」还有{_countdown(target, now)}截止。\n"
                    f"截止时间：{a['deadline']}\n"
                    f"想参加的话，记得在截止前用 /报名 {a['id']} 登记。"
                ),
            })
    # 公共比赛：字段是 official_ 前缀，映射到通用提醒类型
    _COMP_FIELDS = {"reg_deadline": "official_reg_deadline"}
    for c in competitions:
        if c.get("status") != "confirmed":
            continue
        # 通知范围：该比赛指定了通知群就只发这些群，否则发全部允许群。
        # 这样「周赛」可以只发集训队群，不打扰新生群。
        # 读取失败不能退回全群广播；该比赛留待下轮，其余对象继续。
        try:
            targets = get_competition_target_groups(c["id"])
        except (sqlite3.Error, OSError):
            logger.warning("比赛 #%s 的通知范围无法读取，跳过本轮提醒", c["id"])
            continue
        if not targets:
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
                            f"【报名提醒】「{c['name']}」还有{_countdown(target, now)}截止报名。\n"
                            f"报名截止：{c[field]}\n"
                            + (f"官网：{c['official_site']}\n" if c.get('official_site') else "")
                            + "想参赛的同学记得确认报名结果。"
                        ),
                    })
    return out


def _delivery_key(item: dict) -> str:
    # 键必须包含群号：同一对象的提醒要为每个允许群各发一条，
    # 不带群号会导致多群订阅时只有第一个群收到。
    # 键还必须包含提醒节点：同一目标时间要按 24h/1h 等多个节点各发一次，
    # 不带节点会让第一个节点发完就把后续节点一起吞掉。
    return delivery_dedup_key(item["group_id"], item["obj_type"], item["obj_id"],
                              item["remind_type"], item["target_str"],
                              item.get("offset_hours"))


def build_training_reminders(sessions: list[dict], now: datetime) -> list[dict]:
    """按课程的具体开课日期生成群提醒；每周实例独立去重。

    节点为 ``REMIND_OFFSET_HOURS`` 与 ``TRAINING_REMIND_EXTRA_OFFSETS_MINUTES``
    的并集（含 30/15 分钟级），并尊重群级「课程提醒」开关。
    """
    out = []
    offsets = training_offset_hours()
    horizon = max(offsets)
    for session in sessions:
        group_id = session.get("group_id")
        if group_id not in config.ALLOWED_GROUPS:
            continue
        if not group_course_remind_enabled(group_id):
            continue
        for target in training_occurrence_times(session, now, horizon):
            offset = _due_offset_hours(target - now, offsets)
            if offset is None:
                continue
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
                "offset_hours": offset,
                "text": (
                    f"【提醒】课程「{session['title']}」开始：{target_str}\n"
                    f"地点：{session.get('location') or '待定'}"
                    + ("\n" + "；".join(details) if details else "")
                    + "\n记得留好时间，我们课上见。"
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
        if _platform_paused(item["group_id"]):
            continue
        key = _delivery_key(item)
        token = claim_delivery(
            item["group_id"], item["obj_type"], item["obj_id"], item["remind_type"], item["target_str"],
            now=current_time(), max_attempts=max_attempts, lease_seconds=_DELIVERY_LEASE_SECONDS,
            allowed_groups=set(config.ALLOWED_GROUPS), offset_hours=item.get("offset_hours"),
        )
        if token is None or not validate_delivery_claim(
            key, token, now=current_time(), allowed_groups=set(config.ALLOWED_GROUPS),
        ):
            continue
        ok, error, error_code = await _send_reminder_result(send_group, item["group_id"], item["text"])
        # 接收到成功回执即先持久化事实，后续状态收尾抛错也不重复发送。
        try:
            receipt_saved = ok and record_delivery_success(key, token, now=current_time())
        except sqlite3.Error:
            logger.exception("消息已发送但成功事实写入失败，保留 sending 等待恢复: %s", key)
            continue
        try:
            finish_delivery(key, token, ok, error=error, error_code=error_code,
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
