"""CDUTETC ACM 机器人主程序。

职责：连接 OneBot 或 QQ 官方机器人，路由群消息：
  - 命令式操作（/修正、/征集、/报名、/征集汇总、/征集关闭 等）→ commands 模块分发
  - 「发布 <链接/信息>」→ 抓取 → 提取 → 默认私聊超管确认 → 按群通知
  - @ 本机器人的自然语言提问 → FAQ / 资料 / 受限模型；纯寒暄默认本地回复
  - 周期提醒：比赛临近 + 征集登记截止 + 训练开始
  - 周期抓取：公共来源低频抓取（默认关闭）+ 变化检测 + 待审更新
  - 自动周赛：每周日按模板生成记录并提醒（默认不通知全员，超管确认后处理）

安全要点：
  - 群与发布者白名单默认全拒绝；权限校验先于抓取/调模型/写库。
  - 身份一律取自协议事件字段，不信消息正文。
  - 主动消息由显式开关控制；仅 QQ 返回有效消息 id 才确认发送成功。
  - 跨群通知按目标群路由，发送失败不标记去重，也不声称已通知成功。

扩展指南：
  - 新增消息指令：commands.py 的 handlers 表里加分支
  - 新增定时任务：on_ready 里 self.sched.add_job(...)
"""

import asyncio
import contextvars
import json
import logging
import re
import sqlite3
import time
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from cdutetc.clients.base import BaseBotClient, capabilities_of
from cdutetc.clients.onebot import OneBotClient, OneBotMessage

from cdutetc import commands
from cdutetc import config
from cdutetc import content_filter
from cdutetc import crawler
from cdutetc import database as db
from cdutetc import llm
from cdutetc import roles
from cdutetc import notifications
from cdutetc.errors import AppErrorCode, DeliveryMode, DeliveryResult, result_from_exception, safe_error_hint
from cdutetc.contest_fields import map_competition_info
from cdutetc.database import create_published_competition_once, init_db
from cdutetc.fetcher import FetchError, fetch_text
from cdutetc.llm import extract_contest, normalize_time
from cdutetc.publish_text import is_timed_contest_announcement, parse_simple_publication
from cdutetc.reminder import check_general_reminders, check_contest_start_reminders
from cdutetc.utils import (
    admin_group_id,
    can_manage_group,
    extract_url,
    is_addressed_to_bot,
    is_allowed_group,
    match_publish_command,
    message_mentions,
    is_super_admin,
    mentioned_user_ids,
    parse_command,
    strip_at,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")
_reply_sequence = contextvars.ContextVar("qq_reply_sequence", default=0)
_private_reply_context = contextvars.ContextVar("qq_private_reply_context", default=None)

# 官方协议严格限制被动回复 msg_seq 在 [1,5]，第 6 条会被平台判为 INVALID_REQUEST。
_MAX_PASSIVE_REPLY_SEQ = 5
# 溢出文本合并到最后一帧后的最大字符数，避免超出平台单条长度上限。
_MERGED_REPLY_MAX_CHARS = 1500


def _clamp_reply_seq(seq: int) -> int:
    """被动回复序号硬封顶：1 <= seq <= 5。"""
    return max(1, min(int(seq), _MAX_PASSIVE_REPLY_SEQ))


def _merge_group_reply_texts(texts: list[str]) -> list[str]:
    """同一条入站消息的同群回复超过平台上限时，把溢出部分合并进最后一帧。"""
    if len(texts) <= _MAX_PASSIVE_REPLY_SEQ:
        return list(texts)
    head = list(texts[:_MAX_PASSIVE_REPLY_SEQ - 1])
    merged = "\n".join(texts[_MAX_PASSIVE_REPLY_SEQ - 1:])
    if len(merged) > _MERGED_REPLY_MAX_CHARS:
        merged = merged[:_MERGED_REPLY_MAX_CHARS]
    head.append(merged + "\n[消息过长已折叠]")
    return head


def _notification_summary(sent: int, total: int) -> str:
    if not config.GROUP_ACTIVE_MESSAGES_ENABLED and sent == 0:
        return "群通知未发送：主动群消息未启用，请联系管理员。"
    if sent == total:
        return f"群通知已发送：{sent}/{total} 个目标群。"
    if sent == 0:
        return f"群通知未确认发送：0/{total} 个目标群，请管理员检查平台权限或限额。"
    return (
        f"群通知已发送：{sent}/{total} 个目标群；"
        f"其余 {total - sent} 个群未确认发送，请管理员检查平台权限或限额。"
    )


def _build_publish_notice(info: dict, cid: int) -> str:
    """构造发往各群的新比赛公告（字段全部取自已确认的录入内容）。"""
    lines = [f"【新比赛发布】#{cid} {info.get('name') or '未知'}"]
    if info.get("edition"):
        lines.insert(1, f"届次：{info['edition']}")
    for label, key in (("报名开始", "official_reg_start"),
                       ("报名截止", "official_reg_deadline"),
                       ("比赛时间", "official_time"),
                       ("赛道", "tracks"),
                       ("设备要求", "device_req")):
        value = (info.get(key) or "").strip()
        if value:
            lines.append(f"{label}：{value}")
    if (info.get("summary") or "").strip():
        lines.append(f"简介：{info['summary'].strip()}")
    if (info.get("official_site") or "").strip():
        lines.append(f"官网：{info['official_site'].strip()}")
    lines.append("请及时查看并提前做好准备~")
    return "\n".join(lines)


_TIME_FIELD_LABELS = (
    ("official_reg_start", "报名开始"),
    ("official_reg_deadline", "报名截止"),
    ("official_time", "比赛时间"),
)


def _sanitize_publish_times(info: dict) -> tuple[dict, list[str]]:
    """把解析不了的时间降级为空串，并把原文记进简介，绝不因此拒绝保存。

    校赛常见「下学期开学初」这类表述，数据库只接受 YYYY-MM-DD HH:MM。
    强行入库会在超管同意时报错，因此在这里先降级：时间留空（=待定），
    原文写进简介，回执里也提示出来，等日期确定后再 /修正 补。
    """
    cleaned = dict(info)
    notes: list[str] = []
    for key, label in _TIME_FIELD_LABELS:
        raw = (cleaned.get(key) or "").strip()
        if not raw:
            continue
        normalized = normalize_time(raw)
        if normalized is None:
            notes.append(f"{label}待定：{raw}")
            cleaned[key] = ""
        else:
            cleaned[key] = normalized
    if notes:
        existing = (cleaned.get("summary") or "").strip()
        merged = "；".join(notes)
        cleaned["summary"] = f"{existing}\n{merged}".strip() if existing else merged
    return cleaned, notes


def _format_remind_offsets() -> str:
    """开赛提醒以专用分钟配置为准，不能显示旧版24h/1h节点。"""
    return "、".join(f"{m} 分钟" for m in config.CONTEST_REMIND_OFFSETS_MINUTES)


# 发布回执里展示的字段顺序（名称单独处理）
_RECEIPT_FIELDS = (
    ("届次", "edition"),
    ("报名开始", "official_reg_start"),
    ("报名截止", "official_reg_deadline"),
    ("比赛时间", "official_time"),
    ("赛道", "tracks"),
    ("设备要求", "device_req"),
    ("官网", "official_site"),
    ("简介", "summary"),
)


def _build_publish_receipt(info: dict, *, status: str, ref: int) -> str:
    """构造发布回执：清楚告诉发布者「存了什么、缺什么、下一步是什么」。

    status 取值「待超管确认」或「已入库」；ref 为申请编号或比赛编号。
    """
    filled, missing = [], []
    for label, key in _RECEIPT_FIELDS:
        value = (info.get(key) or "").strip()
        if value:
            filled.append(f"{label}：{value}")
        else:
            missing.append(label)

    lines = [f"✅ 已保存：{info.get('name') or '未命名比赛'}"]
    lines.append(f"编号：#{ref}（{status}）")
    lines.extend(filled)
    if missing:
        lines.append(f"暂无信息：{'、'.join(missing)}（不影响使用，后续可用 /修正 补充）")

    remind_time = (info.get("official_time") or "").strip()
    if remind_time:
        if status == "待超管确认":
            lines.append(f"提醒：超管同意后安排开始前 {_format_remind_offsets()}的未来节点，已错过的节点不补发")
        else:
            lines.append(f"提醒：按开始前 {_format_remind_offsets()}的未来有效节点发到群里")
    else:
        lines.append("提醒：时间未定，补上比赛时间后才会自动提醒")
    return "\n".join(lines)


def _build_publish_review(pending: dict) -> str:
    """构造发给超管的待确认申请正文（含全部已整理字段与查重提示）。"""
    info = json.loads(pending.get("fields_json") or "{}")
    lines = [f"【待确认发布】#{pending['id']}", f"比赛：{info.get('name') or '未知'}"]
    for label, key in (("届次", "edition"),
                       ("报名开始", "official_reg_start"),
                       ("报名截止", "official_reg_deadline"),
                       ("比赛时间", "official_time"),
                       ("赛道", "tracks"),
                       ("设备要求", "device_req"),
                       ("简介", "summary"),
                       ("官网", "official_site")):
        value = (info.get(key) or "").strip()
        lines.append(f"{label}：{value or '未提供'}")
    if (info.get("official_time") or "").strip():
        lines.append(f"提醒计划：同意后安排开始前 {_format_remind_offsets()}的未来节点，已错过的不补发")
    lines.append(f"来源群：{db.describe_account('group', pending.get('group_id') or '')}")
    lines.append(f"提交人：{db.describe_account('user', pending.get('submitted_by') or '')}")
    if pending.get("duplicate_note"):
        lines.append(f"⚠ 查重提示：{pending['duplicate_note']}")
    else:
        lines.append("查重：未发现名称+届次+比赛时间完全一致的记录")
    lines.append("")
    lines.append(f"回复「同意 {pending['id']}」入库并通知群，或「拒绝 {pending['id']}」丢弃。")
    return "\n".join(lines)


_PUBLISH_DECISION_RE = re.compile(
    r"^(?P<action>同意|通过|批准|拒绝|驳回|approve|reject|ok|y|n)"
    r"\s*(?P<pid>\d+)?\s*(?P<note>.*)$",
    re.IGNORECASE,
)


def _apply_publish_scope(cid: int, group_id: str) -> None:
    """新发布的比赛默认只通知「发布它的群」。

    这样集训队群发的比赛不会打扰新生群，反之亦然。
    想通知多个群时用 /通知群 <id> 群号,群号 扩展。
    ACM_PUBLISH_SCOPE_TO_SOURCE=0 可恢复「发布即通知全部群」的旧行为。
    """
    if not config.PUBLISH_SCOPE_TO_SOURCE or not group_id:
        return
    try:
        db.set_competition_notify_groups(cid, {group_id}, operator="publish:scope")
    except (sqlite3.Error, ValueError) as e:
        logger.warning("发布通知范围设置失败: %s", e)


def _extend_scope_with_group(cid: int, group_id: str) -> bool:
    """把某个群并入比赛通知范围。返回是否真的新增了群。

    范围原本为空（=全部允许群）时不改动，返回 False。
    """
    if not group_id:
        return False
    try:
        scope = db.get_competition_notify_groups(cid)
    except (sqlite3.Error, ValueError):
        return False
    if not scope or group_id in scope:
        return False
    scope.add(group_id)
    try:
        db.set_competition_notify_groups(cid, scope, operator="publish:extend")
    except (sqlite3.Error, ValueError) as e:
        logger.warning("扩展通知范围失败: %s", e)
        return False
    return True

# 这些命令不依赖具体群，未配置任何允许群时也要放行（否则无法引导加第一个群）
_GROUP_FREE_COMMANDS = frozenset(
    {"加群", "删群", "群列表", "加老师", "删老师", "老师列表", "帮助", "help"}
)

# 未授权群提示的冷却时间记录：{group_id: 上次提示的 monotonic 时间}
_unauthorized_notice_at: dict[str, float] = {}


def _weekly_contest_window(now: datetime):
    """返回下一场尚未开赛的周赛及其所属周；跨周生成不误建过去场次。"""
    tz = ZoneInfo(config.TZ_NAME)
    now = now.replace(tzinfo=tz) if now.tzinfo is None else now.astimezone(tz)
    if not 0 <= config.WEEKLY_CONTEST_WEEKDAY <= 6:
        raise ValueError("周赛星期必须在 0（周一）到 6（周日）之间")
    week_start = now.date() - timedelta(days=now.weekday())
    contest_date = week_start + timedelta(days=config.WEEKLY_CONTEST_WEEKDAY)
    start = datetime.combine(
        contest_date, datetime.strptime(config.WEEKLY_CONTEST_START, "%H:%M").time(), tzinfo=tz,
    )
    end = datetime.combine(
        contest_date, datetime.strptime(config.WEEKLY_CONTEST_END, "%H:%M").time(), tzinfo=tz,
    )
    if end <= start:
        raise ValueError("周赛结束时间必须晚于开始时间")
    if start <= now:
        week_start += timedelta(days=7)
        start += timedelta(days=7)
        end += timedelta(days=7)
    return week_start, start, end


class ACMBot:
    """CDUTETC ACM 机器人：共用业务路由与双协议发送接口。"""

    def __init__(self, client: BaseBotClient | None = None):
        self.client = client
        self.sched: AsyncIOScheduler | None = None
        # 五层防护-4：频率防刷限流器（单用户滑动窗口，参数来自 config）
        self._qa_rate_limiter = content_filter.RateLimiter(
            config.QA_RATE_LIMIT_MAX_CALLS,
            config.QA_RATE_LIMIT_SECONDS,
        )
        if client is not None:
            client.group_msg_handler = self.on_group_at_message_create
            client.private_msg_handler = self.on_private_message_create
            client.on_ready = self._on_ready

    # ---------- 生命周期 ----------

    async def _on_ready(self, now: datetime | None = None):
        """连接成功后补未来周赛并立即检查提醒，再开始周期任务（幂等）。"""
        if self.sched is not None:
            logger.info("机器人已上线（调度器已存在，跳过重复创建）")
            return
        init_db()
        # 合并 .env 基线与数据库里的动态白名单（超管 /加群 /加老师 添加的条目）
        roles.refresh()
        tz = ZoneInfo(config.TZ_NAME)
        now = now or datetime.now(tz)
        now = now.replace(tzinfo=tz) if now.tzinfo is None else now.astimezone(tz)
        sched = AsyncIOScheduler(timezone=config.TZ_NAME)
        # legacy 比赛提醒（contests/reminders 旧表）已下线；此处只跑通用提醒
        # （征集截止 / 公共比赛 / 课程开课），扫描节奏沿用原配置项。
        sched.add_job(
            self._general_remind_job,
            "interval",
            minutes=config.REMIND_CHECK_INTERVAL_MINUTES,
            id="general-remind-check",
            next_run_time=now,
        )
        sched.add_job(
            self._contest_remind_job,
            "interval",
            seconds=config.CONTEST_REMIND_CHECK_SECONDS,
            id="contest-remind-check",
            next_run_time=now,
            max_instances=1,
            coalesce=True,
        )
        sched.add_job(
            self._crawl_job,
            "interval",
            minutes=config.FETCH_MIN_INTERVAL_MINUTES,
            id="source-crawl",
        )
        if config.WEEKLY_CONTEST_ENABLED:
            _, start, _ = _weekly_contest_window(now)
            # 提醒窗口打开前一个扫描周期预生成，避免开赛时才生成及同刻调度竞争。
            generation_at = start - timedelta(
                hours=max(config.REMIND_BEFORE_HOURS, *config.REMIND_OFFSET_HOURS,
                          max(config.CONTEST_REMIND_OFFSETS_MINUTES) / 60),
                minutes=config.REMIND_CHECK_INTERVAL_MINUTES,
            )
            sched.add_job(
                self._weekly_contest_job,
                "cron",
                day_of_week=generation_at.weekday(),
                hour=generation_at.hour,
                minute=generation_at.minute,
                id="weekly-contest",
            )
            await self._weekly_contest_job(now=now)
        sched.start()
        self.sched = sched
        logger.info(
            "机器人已上线：每 %d 分钟检查提醒，每 %d 分钟抓取公共来源%s",
            config.REMIND_CHECK_INTERVAL_MINUTES,
            config.FETCH_MIN_INTERVAL_MINUTES,
            "，自动周赛已开启（%s %s-%s）" % (
                "周日" if config.WEEKLY_CONTEST_WEEKDAY == 6 else f"星期{config.WEEKLY_CONTEST_WEEKDAY + 1}",
                config.WEEKLY_CONTEST_START,
                config.WEEKLY_CONTEST_END,
            ) if config.WEEKLY_CONTEST_ENABLED else "，自动周赛关闭",
        )

    # ---------- 私聊消息路由 ----------

    async def on_private_message_create(self, message: OneBotMessage):
        """私聊消息：超管可发 /命令 私聊管理（FAQ/课程/校安排等），其余回执验证链路。
        默认不记录私聊原文；初次部署采集白名单时设置 ACM_DEBUG_LOG_IDENTITY=1 临时开启。"""
        token = _private_reply_context.set((str(message.user_id), str(getattr(message, "id", "") or ""), 0))
        try:
            uid = message.user_id
            content = message.content or ""
            if config.DEBUG_LOG_IDENTITY:
                logger.info("[私聊][调试] user_id=%s content=%s", uid, content[:50])
            # 超管私聊回复「同意/通过/拒绝 <编号>」处理待确认的发布申请
            if is_super_admin(uid):
                handled = await self._handle_publish_decision(uid, content)
                if handled:
                    return
            if content.strip().startswith("/") and is_super_admin(uid):
                cmd, args = parse_command(content)
                from cdutetc import group_admin
                if cmd in group_admin.COMMANDS:
                    replies = group_admin.handle_command(cmd, args, uid)
                    if any(reply.get("refresh_weekly") for reply in replies or []):
                        await self._weekly_contest_job()
                    for reply in replies or []:
                        await self._send_private_result(uid, reply["text"])
                    return
                if cmd == "通知测试":
                    await self._send_private_result(uid, await self._notification_test(uid, args))
                    return
                admin_gid = admin_group_id()
                if not admin_gid:
                    # 引导场景：一个群都没配时，仍要允许超管用 /加群 把第一个群加进来，
                    # 否则会陷入“没群 → 不能用命令 → 永远加不了群”的死锁。
                    cmd, _ = parse_command(content)
                    if cmd not in _GROUP_FREE_COMMANDS:
                        await self._send_private_result(
                            uid,
                            "未配置允许群，无法定位管理群（可用 /加群 <群号> 添加，"
                            "或设置 ACM_ADMIN_GROUP / ACM_ALLOWED_GROUPS）",
                        )
                        return
                replies = await commands.handle_command(content, admin_gid, uid)
                for reply in replies or []:
                    if reply.get("text"):
                        await self._send_private_result(uid, reply["text"])
                return
            from cdutetc import group_admin
            if is_super_admin(uid):
                await self._send_private_result(uid, group_admin.answer_private(content, uid))
            else:
                await self._send_private_result(uid, "你好～群内问题请在群里 @我，私聊管理仅向超管开放。")
        except Exception:
            logger.exception("私聊处理失败 [%s]", AppErrorCode.INTERNAL)
        finally:
            _private_reply_context.reset(token)

    async def _handle_publish_decision(self, uid: str, content: str) -> bool:
        """处理超管私聊的发布审批回复。返回 True 表示已处理并已回复过。

        可接受：`同意` / `同意 3` / `通过 3` / `拒绝 3` / `拒绝 3 原因`。
        不带编号时，若只有一条待确认申请则直接作用于它，否则列出清单。
        """
        match = _PUBLISH_DECISION_RE.match((content or "").strip())
        if not match:
            return False
        approve = match.group("action") in ("同意", "通过", "批准", "approve", "ok", "y")
        pid_text = match.group("pid")
        note = (match.group("note") or "").strip()
        pending = db.list_pending_publications()
        if not pending:
            await self._send_private_result(uid, "当前没有待确认的发布申请。")
            return True
        if pid_text:
            pid = int(pid_text)
        elif len(pending) == 1:
            pid = pending[0]["id"]
        else:
            lines = ["有多条待确认申请，请在回复里带上编号："]
            for item in pending:
                lines.append(f"#{item['id']} {item['name'] or '未命名'}｜{item['official_time'] or '时间待定'}")
            lines.append("例如：同意 3")
            await self._send_private_result(uid, "\n".join(lines))
            return True
        try:
            if approve:
                cid, created = db.approve_pending_publication(pid, operator=uid)
                pending_item = db.get_pending_publication(pid) or {}
                if created:
                    await self._send_private_result(
                        uid, f"已入库：#{cid} {pending_item.get('name') or ''}。"
                        f"已自动安排开赛前 {_format_remind_offsets()}的未来有效节点；"
                        "时间未定或已过节点不建计划。正在通知目标群。"
                    )
                    await self._notify_publish_group(pending_item, cid, uid)
                else:
                    await self._send_private_result(
                        uid, f"该比赛已存在（编号 {cid}），未重复入库；"
                        "通知范围和未来提醒计划已按本次申请同步。"
                    )
            else:
                if not db.reject_pending_publication(pid, operator=uid, note=note):
                    await self._send_private_result(uid, f"申请 #{pid} 已处理过，无需重复操作。")
                    return True
                await self._send_private_result(uid, f"已拒绝申请 #{pid}，未入库。")
        except ValueError:
            await self._send_private_result(uid, safe_error_hint(AppErrorCode.VALIDATION))
        return True

    async def _notify_publish_group(self, pending_item: dict, cid: int, operator_id: str = "") -> None:
        """超管同意后发送新比赛通知：只发给该比赛的通知范围（默认仅来源群）。"""
        info = json.loads(pending_item.get("fields_json") or "{}")
        notice = _build_publish_notice(info, cid)
        try:
            targets = sorted(db.get_competition_target_groups(cid))
        except (sqlite3.Error, ValueError):
            targets = []
        results = []
        for gid in targets:
            results.append(await self._send_group_result(gid, notice))
        failures = [result for result in results if not result.ok]
        if failures:
            codes = sorted({getattr(result.code, "value", str(result.code)) for result in failures})
            logger.warning("比赛 #%s 群通知结果失败码=%s", cid, ",".join(codes))
            await self._send_private_result(
                operator_id,
                f"比赛 #{cid} 已获批并保存提醒计划，但群通知未全部确认送达："
                + "、".join(f"[{code}]" for code in codes),
            )

    # ---------- 群消息路由 ----------

    async def on_group_at_message_create(self, message: OneBotMessage):
        reply_token = _reply_sequence.set(0)
        try:
            group_id = message.group_id
            # 默认不记录群号/QQ号/消息内容，避免长期留存个人信息；
            # 初次部署采集白名单时设置 ACM_DEBUG_LOG_IDENTITY=1 临时开启。
            if config.DEBUG_LOG_IDENTITY:
                logger.info(
                    "[身份采集][调试] group_id=%s user_id=%s content=%s",
                    group_id,
                    getattr(message, "user_id", "") or "",
                    (message.content or "")[:50],
                )
            if not is_allowed_group(group_id, config.ALLOWED_GROUPS):
                logger.info("非授权群消息已忽略: %s", group_id)
                await self._notice_unauthorized_group(message, group_id)
                return
            # 本机账号优先取客户端上报的真实标识，ACM_BOT_QQ 只作兜底。
            # 顺序反了会导致配错 BOT_QQ 后所有 @ 都不被识别、普通提问被静默丢弃。
            bot_qq = self.bot_identity
            # 统一读适配层规范化的 message.mentions：官方协议采信平台 is_you，
            # OneBot 用 CQ 码里的 QQ 号与自身上报账号比对。
            addressed = is_addressed_to_bot(message, bot_qq)
            targets = {item["id"] for item in message_mentions(message) if item["id"]}
            # 带 @ 的消息只有指向本机器人时才受理，包括发给他人的命令。
            if targets and not addressed:
                logger.info(
                    "群消息 @ 的不是本机器人，已忽略（@目标=%s，本机=%s）",
                    ",".join(sorted(targets)), bot_qq or "未知",
                )
                return
            text = strip_at(message.content)
            if not text:
                return
            # 群内默认必须 @ 本机器人 才响应（含命令），避免抢答他人命令；
            # ACM_GROUP_REQUIRE_AT=0 时回退为“无 @ 也受理显式命令”。
            if config.GROUP_REQUIRE_AT:
                allowed_without_at = False
            else:
                allowed_without_at = (
                    text.startswith("/")
                    or match_publish_command(text, config.PUBLISH_CMD) is not None
                )
            if not addressed and not allowed_without_at:
                logger.info(
                    "未 @ 本机器人，已忽略（消息含 @ 段：%s，本机=%s，文本长度 %d）",
                    "[CQ:at" in (message.content or ""),
                    bot_qq or "未知",
                    len(text),
                )
                return
            member_id = self._extract_member_id(message)
            publish_payload = match_publish_command(text, config.PUBLISH_CMD)
            if publish_payload is not None:
                await self._handle_publish(message, publish_payload, group_id, member_id)
                return
            cmd, args = parse_command(text)
            if cmd == "通知测试":
                await self._safe_reply(message, await self._notification_test(member_id, args, message=message))
                return
            replies = await commands.handle_command(text, group_id, member_id)
            if any(reply.get("refresh_weekly") for reply in replies or []):
                await self._weekly_contest_job()
            if replies is None:
                # 先限流和检查输入，再考虑模型意图识别，避免被拒绝的问题仍产生费用。
                allowed, gate_reply = self._qa_gate(member_id, text)
                if not allowed:
                    if gate_reply:
                        await self._safe_reply(message, gate_reply)
                    return
                # 老师/超管用自然语言描述比赛（没带「发布」）时，先让模型判断意图，
                # 判为「在发布比赛」就直接走发布流程，不必死记命令格式。
                if (
                    config.NL_INTENT_ENABLED
                    and not text.startswith("/")
                    and not content_filter.is_social_chat(text)
                    and can_manage_group(member_id)
                ):
                    intent = "add_contest" if is_timed_contest_announcement(text) else await llm.classify_intent(text)
                    if intent == "add_contest":
                        logger.info("识别为发布比赛意图，转发布流程")
                        await self._handle_publish(message, text, group_id, member_id)
                        return
                replies = await commands.handle_non_command_message(
                    text, group_id, member_id, staff=can_manage_group(member_id)
                )
                # 五层防护-5：输出过滤（FAQ/模型回答统一二次敏感检查、长度截断、禁超长链接）
                if replies:
                    replies = [
                        {**reply, "text": content_filter.sanitize_output(
                            reply.get("text", ""),
                            max_length=config.QA_OUTPUT_MAX_LENGTH,
                            max_link_length=config.QA_OUTPUT_MAX_LINK_LENGTH,
                        )}
                        for reply in replies
                        if reply.get("text")
                    ]
            cross_sent = 0
            cross_total = 0
            admin_notice_texts = []
            same_group_texts = [
                reply["text"] for reply in (replies or [])
                if reply.get("text") and reply.get("group_id", group_id) == group_id
            ]
            # 官方协议被动回复最多 5 条：超出的同群文本先合并进最后一帧再发，
            # 严禁直接发起第 6 条 _safe_reply（会被拒为 INVALID_REQUEST）。
            # OneBot 无此约束，保持原有逐条回复行为。
            if self.supports_passive_reply:
                merged_texts = _merge_group_reply_texts(same_group_texts)
            else:
                merged_texts = same_group_texts
            merged_cursor = 0
            for reply in replies:
                if not reply.get("text"):
                    continue
                target_group = reply.get("group_id", group_id)
                if target_group == group_id:
                    if merged_cursor < len(merged_texts):
                        await self._safe_reply(message, merged_texts[merged_cursor])
                        merged_cursor += 1
                else:
                    cross_total += 1
                    if (await self._send_group_result(target_group, reply["text"])).ok:
                        cross_sent += 1
                if reply.get("notify_super_admin"):
                    admin_notice_texts.append(reply["text"])
            if admin_notice_texts:
                notice = "管理变更确认：\n" + "\n".join(admin_notice_texts)
                sent = 0
                for admin in sorted(config.SUPER_ADMIN):
                    try:
                        if (await self._send_private_result(admin, notice)).ok:
                            sent += 1
                    except Exception:
                        logger.warning("管理变更超管确认发送异常", exc_info=True)
                if sent == 0:
                    await self._safe_reply(
                        message,
                        "操作已保存，但未能私聊确认超管；请超管查看 /群列表 或 /老师列表。",
                    )
                elif sent < len(config.SUPER_ADMIN):
                    await self._safe_reply(
                        message,
                        f"操作已保存，超管私聊确认仅送达 {sent}/{len(config.SUPER_ADMIN)} 人；"
                        "未收到者可查看 /群列表 或 /老师列表。",
                    )
            if cross_total:
                await self._safe_reply(message, _notification_summary(cross_sent, cross_total))
        except Exception:
            logger.exception("处理群消息失败")
            await self._safe_reply(message, safe_error_hint(AppErrorCode.INTERNAL))
        finally:
            _reply_sequence.reset(reply_token)

    # ---------- 内部处理 ----------

    # ---------- 接入能力位（唯一协议差异出口） ----------

    @property
    def capabilities(self):
        """当前接入客户端的能力位；缺失字段按配置协议兜底。"""
        return capabilities_of(self.client)

    @property
    def supports_passive_reply(self) -> bool:
        """是否支持被动回复：决定是否合并多帧、是否记忆群消息格式。"""
        return self.capabilities.supports_passive_reply

    @property
    def supports_markdown(self) -> bool:
        """主动群消息是否支持 markdown 消息体。"""
        return self.capabilities.supports_markdown

    @property
    def protocol(self) -> str:
        """接入协议标识，仅作数据库记录键与日志，不参与分支判断。"""
        return self.capabilities.protocol

    @property
    def bot_identity(self) -> str:
        """本机器人自身账号，用于判断群里 @ 的是不是自己。

        优先取客户端上报的自身标识（真实账号，最可信），其次用配置兜底；
        不再读原始事件里的 self_id（适配层不保证保留该字段）。
        """
        return str(
            getattr(self.client, "self_id", "")
            or getattr(self.client, "bot_qq", "")
            or config.BOT_QQ
            or ""
        )

    def _extract_member_id(self, message: OneBotMessage) -> str:
        """提取消息成员标识。

        两套协议在适配层已把成员账号统一挂到 user_id 上（OneBot 即 QQ 号，
        官方为 openid），无需再猜 member_openid / author_id 等旧字段。
        """
        return str(getattr(message, "user_id", "") or "")

    def _qa_gate(self, member_id: str, text: str) -> tuple[bool, str | None]:
        """非命令消息进入问答链路前的输入侧闸门（五层防护中的 3/2/2.5/4 层）。

        Returns:
            (True, None)     放行进入问答链路；
            (False, 话术)    拦截并回复话术（敏感内容 / 频率超限）；
            (False, None)    拦截且忽略不回复（黑名单 / 非白名单成员）。
        话题范围硬限制、算法话题拦截与输出过滤分别在 llm.answer_question、
        commands.handle_non_command_message 与发送前完成。
        """
        # 五层防护-3：用户权限。黑名单命中直接忽略；
        # 仅当显式配置了 ACM_QA_WHITELIST 时才限制为白名单成员。
        # 注意：不要再回退到「老师 ∪ 超管」——那会让配了老师白名单的部署
        # 变成“只有管理员能提问”，普通成员被静默忽略。
        if config.QA_BLACKLIST and member_id in config.QA_BLACKLIST:
            logger.info("问答已忽略（黑名单成员）: %s", member_id)
            return False, None
        if (
            config.QA_WHITELIST_ENABLED
            and config.QA_WHITELIST
            and member_id not in config.QA_WHITELIST
        ):
            logger.info("问答已忽略（非白名单成员）: %s", member_id)
            return False, config.QA_WHITELIST_REPLY

        # 五层防护-2：敏感内容拦截（输入侧，llm 层另有双保险）
        if config.QA_SENSITIVE_GATE_ENABLED and content_filter.has_sensitive(text):
            logger.info("问答已拒绝（输入含敏感内容）: %s", member_id)
            return False, config.QA_SENSITIVE_REPLY

        # 五层防护-4：频率防刷（单用户滑动窗口限流）
        if config.QA_RATE_LIMIT_ENABLED and not self._qa_rate_limiter.allow(member_id):
            logger.info("问答已限流（成员 %s）", member_id)
            return False, config.QA_RATE_LIMIT_REPLY

        return True, None

    async def _handle_publish(self, message: OneBotMessage, text: str,
                              group_id: str, member_id: str):
        """发布命令：老师/超管权限校验 → 抓取 → 提取 → 校验 → 私聊超管确认。"""
        # 超管继承老师全部权限（含发布），无需再额外加入老师白名单
        if not can_manage_group(member_id):
            await self._safe_reply(message, "抱歉，你没有发布比赛信息的权限~")
            return
        if config.PUBLISH_CONFIRM_ENABLED and not config.SUPER_ADMIN:
            await self._safe_reply(message, "还没配置发布审核人，暂时无法提交比赛，请联系管理员补齐。 [validation]")
            return

        # 有链接就抓官网正文；没有链接也允许发布——直接把老师发来的文字
        # 当作信息源，「有什么先存什么」，不强迫补链接。
        url = extract_url(text) or ""
        if url:
            try:
                page = await fetch_text(url)
            except FetchError as e:
                # 抓取失败不中断：退化为按老师原文提取，保证信息不丢
                logger.warning("链接抓取失败，改为按原文提取: %s", e)
                page = ""
                await self._safe_reply(message, f"链接打不开（{e}），我先按你发来的文字保存。")
        else:
            page = ""

        raw_text = f"链接正文：{page}\n老师原文：{text}" if page else f"老师原文：{text}"
        try:
            info = None if url else parse_simple_publication(text, now=datetime.now(ZoneInfo(config.TZ_NAME)))
        except ValueError as error:
            await self._safe_reply(message, f"比赛时间未保存：{error}，请核对后再发。 [validation]")
            return
        err = None
        if info is None:
            info, err = await extract_contest(raw_text)
        if err is not None or not info:
            await self._safe_reply(
                message,
                f"信息提取失败：{err or '未知原因'}。"
                "可以再发一次，尽量包含比赛名称和时间；实在没有也能先存个名字~",
            )
            return

        info["source_link"] = url
        try:
            competition_info = map_competition_info(info)
        except ValueError as e:
            await self._safe_reply(message, f"信息校验失败：{e}")
            return

        # 时间解析不了（如「下学期开学初」）就降级为待定，原文记入简介，不算失败
        competition_info, _ = _sanitize_publish_times(competition_info)

        # 查重：名称+届次+比赛时间完全一致才判为重复（不看链接，转发消息也能查出）
        duplicate = db.find_duplicate_competition(competition_info)
        if duplicate is not None and duplicate.get("status") == "confirmed":
            cid = duplicate["id"]
            # 同一场比赛在另一个群再次发布：不新建记录，只把本群加入通知范围，
            # 这样「本群发布的比赛在本群提醒」和「一场公开比赛通知多个群」都满足。
            scope = db.get_competition_notify_groups(cid)
            if scope and group_id not in scope:
                scope.add(group_id)
                db.set_competition_notify_groups(cid, scope, operator=f"publish:{member_id}")
                await self._safe_reply(
                    message,
                    f"该比赛已存在（编号 {cid}），已把本群加入通知范围，"
                    "之后的提醒本群也会收到。",
                )
            else:
                await self._safe_reply(
                    message,
                    f"该比赛已存在（编号 {cid}，"
                    f"{duplicate['name']} {duplicate.get('official_time') or '时间待定'}），"
                    "本群已在其通知范围内，本次未重复发布。",
                )
            return

        # 超管确认流程：老师发布 → 整理结果私聊超管 → 同意才入库并通知群
        if config.PUBLISH_CONFIRM_ENABLED:
            try:
                pid = db.create_pending_publication(
                    competition_info,
                    group_id=group_id,
                    source_url=url,
                    source_text=page,
                    submitted_by=member_id,
                    duplicate=duplicate,
                )
            except ValueError as e:
                await self._safe_reply(message, f"提交失败：{e}")
                return
            pending = db.get_pending_publication(pid) or {}
            review = _build_publish_review(pending)
            sent = 0
            failures = []
            for admin in sorted(config.SUPER_ADMIN):
                result = await self._send_private_result(admin, review)
                if result.ok:
                    sent += 1
                else:
                    failures.append(result.code.value)
                    logger.warning("发布申请 #%s 确认私聊推送失败 [%s]", pid, result.code.value)
            receipt = _build_publish_receipt(competition_info, status="待超管确认", ref=pid)
            if not failures:
                receipt += f"\n确认单已向{sent}位超管提交发送，请在私聊回复“同意 {pid}”或“拒绝 {pid}”。"
            else:
                codes = "、".join(f"[{code}]" for code in sorted(set(failures)))
                receipt += (f"\n超管私聊推送失败（成功{sent}/{len(config.SUPER_ADMIN)}）：{codes}。"
                            f"申请 #{pid} 已保留，超管可私聊发送 /待确认列表 后回复“同意 {pid}”。")
            await self._safe_reply(message, receipt)
            logger.info("发布申请 #%s 已提交超管确认：%s", pid, info.get("name"))
            return

        # 显式关闭确认流程时直接发布。
        try:
            cid, created = create_published_competition_once(
                competition_info,
                source_url=url,
                source_text=page,
                operator=f"publish:{member_id}",
                source_group_id=group_id,
            )
        except ValueError as e:
            await self._safe_reply(message, f"信息校验失败：{e}")
            return

        if not created:
            await self._safe_reply(message, f"该比赛已存档（编号 {cid}），本群已在通知范围内。")
            return

        logger.info("已免审发布比赛: %s（群 %s）", info.get("name"), group_id)
        notice = _build_publish_notice(competition_info, cid)
        # 只发该比赛的通知范围（默认即发布它的群），不再全群广播
        try:
            targets = sorted(db.get_competition_target_groups(cid))
        except (sqlite3.Error, ValueError):
            targets = []
        results = [await self._send_group_result(gid, notice) for gid in targets]
        failures = [result for result in results if not result.ok]
        if failures:
            codes = sorted({getattr(result.code, "value", str(result.code)) for result in failures})
            detail = _notification_summary(len(results) - len(failures), len(targets))
            detail += " 失败码：" + ",".join(f"[{code}]" for code in codes)
        else:
            detail = _notification_summary(len(results), len(targets))
        await self._safe_reply(
            message,
            _build_publish_receipt(competition_info, status="已入库", ref=cid)
            + "\n" + detail,
        )

    async def _notification_test(self, uid: str, args: str, *, message=None) -> str:
        """主动发一条测试消息；不复用当前msg_id来冒充主动通道可用。"""
        if not is_super_admin(uid):
            return safe_error_hint(AppErrorCode.PERMISSION)
        parts = args.split()
        formats = {"文本": "text", "text": "text", "markdown": "markdown"}
        requested_format = formats.get(parts[-1].lower()) if parts else None
        if requested_format:
            parts.pop()
        kind = parts[0] if parts else ("群" if message is not None else "私聊")
        if len(parts) > 2 or kind not in ("群", "私聊"):
            return "[validation] 用法：/通知测试 [群|私聊] [目标标识]；官方群测试可在末尾加 文本 或 Markdown"
        if requested_format and (kind != "群" or not self.supports_markdown):
            return "[validation] 文本/Markdown 格式选择仅用于官方群通知测试。"
        default_target = message.group_id if kind == "群" and message is not None else uid
        target = parts[1] if len(parts) == 2 else default_target
        if kind == "群" and message is None and len(parts) < 2:
            return "[validation] 私聊测试群通知时必须指定目标群标识。"
        text = "【通知通道测试】这是主动通知测试消息；收到本条才表示平台已投递到此会话。"
        if kind == "群":
            result = await self._send_group_result(target, text, permission_probe=True,
                                                   message_format=requested_format)
        else:
            # 特意不调用_send_private：该方法可能为当前入站生成被动回复。
            result = await notifications.send_private(self.client, target, text)
        code = getattr(result.code, "value", str(result.code))
        answer = f"通知测试：{'平台已接受' if result.ok else '未确认发送'} [{code}] {result.user_hint}"
        if kind == "群" and result.ok:
            if result.detail == "channel_state_save_failed":
                answer += " [internal] 平台已接受测试，但本地格式和暂停状态保存失败，尚未确认恢复，请检查数据库。"
            else:
                answer += " 主动通道本次测试通过，权限暂停已解除；仍有效的未来提醒会按计划发送。"
        if kind == "群" and self.supports_passive_reply and code not in ("disabled", "target_not_allowed"):
            used_format = requested_format or db.get_group_notification_format(self.protocol, target)
            answer += "\n本次格式：" + ("Markdown" if used_format == "markdown" else "文本")
            if result.ok and result.detail != "channel_state_save_failed":
                answer += "，已记住本群格式，后续主动通知沿用。"
            query = getattr(self.client, "get_group_active_permission", None)
            try:
                state = await asyncio.wait_for(query(target), timeout=6.0) if callable(query) else None
            except Exception:  # noqa: BLE001 - 状态读取失败不能抹掉真实发送回执
                state = None
            if state is not None and state.allowed is not None:
                answer += f"\n平台群设置：allow_proactive_msg={'true' if state.allowed else 'false'}"
                answer += "（允许主动发言）。" if state.allowed else "（当前群不允许主动发言）。"
            else:
                query_code = state.code if state is not None else "unknown"
                if state is not None and state.platform_code is not None:
                    query_code += f"/{state.platform_code}"
                answer += f"\n平台群设置：暂未核实 [{query_code}]；状态接口可能受白名单限制，不能据此认定群开关关闭。"
        return answer

    async def _send_private_result(self, user_id: str, text: str) -> DeliveryResult:
        """当前私聊直接回复显式关联入站ID，其余调用均为主动通知。"""
        context = _private_reply_context.get()
        kwargs = {}
        if context is not None and context[0] == str(user_id):
            uid, msg_id, seq = context
            seq += 1
            _private_reply_context.set((uid, msg_id, seq))
            if msg_id:
                kwargs = {"reply_to": msg_id, "reply_seq": seq}
        try:
            return await notifications.send_private(self.client, user_id, text, **kwargs)
        except Exception as error:
            logger.warning("私聊发送异常 [%s]", type(error).__name__)
            mode = DeliveryMode.PASSIVE if self.supports_passive_reply and kwargs else DeliveryMode.ACTIVE
            return result_from_exception(error, protocol=self.protocol, mode=mode)

    async def _safe_reply(self, message: OneBotMessage, content: str) -> bool:
        """只回复当前入站群消息，官方适配不得复用历史ID或转主动重发。"""
        group_id = message.group_id
        if not is_allowed_group(group_id, config.ALLOWED_GROUPS):
            return False
        seq = _clamp_reply_seq(_reply_sequence.get() + 1)
        _reply_sequence.set(seq)
        result = await notifications.send_group(
            self.client, group_id, content,
            reply_to=str(getattr(message, "id", "") or ""), reply_seq=seq,
        )
        return bool(result)

    async def _notice_unauthorized_group(self, message: OneBotMessage,
                                         group_id: str) -> None:
        """未授权群被明确喊话时，回一句带群号的提示，方便管理员取号后授权。

        只在「@ 了机器人」或「发 /命令」时回复，避免被拉进群后对每条闲聊都插话；
        同一群有冷却时间，防止刷屏。提示本身不提供任何业务功能。
        """
        if not config.NOTICE_UNAUTHORIZED_GROUP:
            return
        raw = message.content or ""
        bot_qq = self.bot_identity
        addressed = is_addressed_to_bot(message, bot_qq)
        text = strip_at(raw)
        if not (addressed or text.startswith("/")):
            return
        now = time.monotonic()
        if now - _unauthorized_notice_at.get(group_id, 0.0) < config.NOTICE_UNAUTHORIZED_INTERVAL:
            return
        _unauthorized_notice_at[group_id] = now
        try:
            seq = _clamp_reply_seq(_reply_sequence.get() + 1)
            _reply_sequence.set(seq)
            await notifications.send_group(
                self.client, group_id,
                "本群尚未授权。\n"
                f"请超管私聊机器人发送：/加群 {group_id}",
                reply_to=str(getattr(message, "id", "") or ""), reply_seq=seq,
            )
        except Exception:  # noqa: BLE001
            logger.warning("未授权群提示发送失败: %s", group_id)

    # ---------- 定时任务回调 ----------

    async def _send_group_result(self, group_id: str, text: str, *, permission_probe: bool = False,
                                 message_format: str | None = None):
        """主动通知唯一业务入口，返回可审计的结构化结果。"""
        from cdutetc.errors import DeliveryCode, DeliveryResult
        if not is_allowed_group(group_id, config.ALLOWED_GROUPS):
            return DeliveryResult(False, DeliveryCode.TARGET_NOT_ALLOWED, self.protocol)
        if not config.GROUP_ACTIVE_MESSAGES_ENABLED:
            return DeliveryResult(False, DeliveryCode.DISABLED, self.protocol)
        if message_format is not None and message_format not in ("text", "markdown"):
            return DeliveryResult(False, DeliveryCode.INVALID_REQUEST, self.protocol,
                                  detail="invalid_message_format")
        if self.supports_passive_reply and not permission_probe:
            block = db.get_group_notification_block(self.protocol, group_id)
            if block:
                return DeliveryResult(False, DeliveryCode.ACTIVE_FORBIDDEN, self.protocol,
                                      detail="active_permission_paused", platform_code=block["platform_code"])
        if self.supports_passive_reply:
            message_format = message_format or db.get_group_notification_format(self.protocol, group_id)
        else:
            message_format = "text"
        if message_format == "markdown":
            result = await notifications.send_group(self.client, group_id, text, message_format=message_format)
        else:
            result = await notifications.send_group(self.client, group_id, text)
        if self.supports_passive_reply:
            if result.code == DeliveryCode.ACTIVE_FORBIDDEN:
                db.block_group_notifications(self.protocol, group_id, result.code.value, result.platform_code)
                logger.warning("QQ主动发送被平台拒绝，已持久暂停该群，需恢复权限后执行 /通知测试 群 验证 [40034105]")
            elif result.code == DeliveryCode.QUOTA_EXHAUSTED:
                # 配额耗尽当天无法恢复，必须熔断，否则30秒/30分钟定时任务会持续无效重试。
                db.block_group_notifications(self.protocol, group_id, result.code.value, result.platform_code)
                logger.warning(
                    "[主动发信限流] 群 %s 今日主动消息配额已耗尽(11253/11263)，停止重试并触发今日熔断",
                    group_id,
                )
            elif result.ok and permission_probe:
                try:
                    db.confirm_group_notification_channel(self.protocol, group_id, message_format)
                except sqlite3.Error:
                    logger.exception("平台已接受主动测试，但本群通知格式和暂停状态保存失败")
                    result = replace(result, detail="channel_state_save_failed")
        return result

    async def _contest_remind_job(self):
        """仅发送已持久化的开赛节点，失败不影响旧版提醒和后续轮次。"""
        if not config.GROUP_ACTIVE_MESSAGES_ENABLED:
            return
        try:
            await check_contest_start_reminders(self._send_group_result)
        except Exception:
            logger.exception("开赛提醒计划扫描失败 [%s]", AppErrorCode.INTERNAL)

    async def _general_remind_job(self):
        """周期检查通用提醒：征集登记截止 + 公共比赛 + 训练开始。"""
        if not config.GROUP_ACTIVE_MESSAGES_ENABLED:
            logger.info("主动群消息未启用，暂停提醒投递")
            return
        try:
            await check_general_reminders(self._send_group_result)
        except Exception:  # noqa: BLE001
            logger.exception("通用提醒扫描失败，等待下一轮重试")

    async def _weekly_contest_job(self, now: datetime | None = None):
        """提前生成下一场未来周赛（默认不通知全员）。

        数据库按周持久化去重，避免重启、重复调度或改名造成重复记录。
        now 供离线验证注入；无时区的注入时间按北京时间处理。
        """
        if not config.WEEKLY_CONTEST_ENABLED:
            return
        template = config.WEEKLY_CONTEST_TEMPLATE
        try:
            now = now or datetime.now(ZoneInfo(config.TZ_NAME))
            week_start, start, end = _weekly_contest_window(now)
            start_str = start.strftime("%Y-%m-%d %H:%M")
            end_str = end.strftime("%Y-%m-%d %H:%M")
            cid, created = db.create_weekly_competition(
                week_start.isoformat(),
                {
                    "name": template,
                    "official_reg_deadline": "",
                    "official_time": start_str,
                    "official_rules": "",
                    "official_site": "",
                    "summary": (
                        "自动周赛（模板生成，默认不通知全员）；"
                        f"时间：{start_str} 至 {end_str}"
                    ),
                },
            )
        except ValueError as e:
            logger.warning("自动周赛生成失败: %s", e)
            return
        # 周赛范围以环境基线和逐群持久开关为准，启动/再次调度也对齐已有记录。
        # 存储原始配置标识；可见和投递时再与授权群求交，空范围始终不广播。
        desired = db.get_weekly_groups()
        try:
            if db.get_competition_notify_groups(cid) != desired:
                db.set_competition_notify_groups(cid, desired, operator="weekly:auto")
        except (sqlite3.Error, ValueError) as e:
            logger.warning("周赛通知群设置失败: %s", e)
        if not created:
            logger.info("该周周赛记录已存在，跳过自动生成（比赛 id=%s）", cid)
            return
        logger.info("已自动生成周赛记录: %s（id=%s），未通知全员", template, cid)

    async def _crawl_job(self):
        """周期抓取公共来源（默认关闭）：变化检测 → 待审更新，不自动广播。"""
        await crawler.run_fetch(send_group=self._send_group_result)


def _build_client():
    """按 ACM_BOT_PROTOCOL 创建接入客户端；两种协议接口完全一致。"""
    if config.BOT_PROTOCOL == "qqbot":
        from cdutetc.clients.qqbot import QQBotClient

        return QQBotClient(
            appid=config.QQBOT_APPID,
            secret=config.QQBOT_SECRET,
            idle_timeout=config.WS_IDLE_TIMEOUT_SECONDS,
        )
    return OneBotClient(
        ws_url=config.ONEBOT_WS_URL,
        token=config.ONEBOT_TOKEN,
        idle_timeout=config.WS_IDLE_TIMEOUT_SECONDS,
    )


def run_bot() -> None:
    """启动入口：配置检查 → 按协议创建客户端 → 常驻连接。"""
    if config.ENV_CREATED:
        need = ("ACM_QQBOT_APPID、ACM_QQBOT_SECRET"
                if config.BOT_PROTOCOL == "qqbot" else "ACM_ONEBOT_WS_URL")
        logger.warning(
            "未找到 .env，已按 .env.example 生成：%s\n"
            "请填写 %s、ACM_LLM_API_KEY、ACM_ALLOWED_GROUPS、"
            "ACM_SUPER_ADMIN、ACM_TEACHER_WHITELIST 后重新启动。",
            config.ENV_FILE, need,
        )
    config.check_config()
    empty = config.describe_empty_config()
    if empty:
        logger.warning("以下配置留空，对应功能不会生效: %s", ", ".join(empty))
    logger.info("接入协议：%s", config.BOT_PROTOCOL)
    client = _build_client()
    ACMBot(client)
    try:
        asyncio.run(client.start())
    except KeyboardInterrupt:
        logger.info("收到中断信号，机器人退出")


if __name__ == "__main__":
    run_bot()
