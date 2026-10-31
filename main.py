"""CDUTETC ACM 机器人主程序。

职责：连接 QQ 官方机器人，路由群消息：
  - 命令式操作（/修正、/征集、/报名、/征集汇总、/征集关闭 等）→ commands 模块分发
  - 「发布 <链接/信息>」命令（老师白名单）→ 抓取 → 提取 → 免审 confirmed → 群通知
  - @ 本机器人的自然语言提问 → FAQ / 模型问答，闲聊和 @ 他人不触发
  - 周期提醒：比赛临近 + 征集登记截止 + 训练开始
  - 周期抓取：公共来源低频抓取（默认关闭）+ 变化检测 + 免审记录（不通知全员）
  - 自动周赛：每周日按模板生成记录并提醒（默认不通知全员，超管确认后处理）

安全要点：
  - 群与发布者白名单默认全拒绝；权限校验先于抓取/调模型/写库。
  - 身份一律取自 SDK 事件字段（group_openid / member_openid），不信消息正文。
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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from onebot_client import OneBotClient, OneBotMessage

import commands
import config
import content_filter
import crawler
import database as db
import roles
from contest_fields import map_competition_info
from database import create_published_competition_once, init_db
from fetcher import FetchError, fetch_text
from llm import extract_contest
from reminder import check_general_reminders, check_reminders
from utils import (
    admin_group_id,
    can_manage_group,
    extract_url,
    is_allowed_group,
    is_publish_command,
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


def _message_sent(result) -> bool:
    """SDK 超时可能返回 None；只有有效消息回执才算成功。"""
    return (
        isinstance(result, dict)
        and isinstance(result.get("id"), str)
        and bool(result["id"].strip())
        and result.get("code") in (None, 0)
    )


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


def _build_confirm(info: dict) -> str:
    """构造存档成功后的确认回复。"""
    return (
        "比赛信息已记录~\n"
        f"比赛：{info.get('name') or '未知'}\n"
        f"报名截止：{info.get('reg_deadline') or '待定'}\n"
        f"比赛时间：{info.get('contest_time') or '待定'}\n"
        f"形式/地点：{info.get('mode') or '待定'} {info.get('location') or ''}\n"
        "有问题随时 @ 我！"
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
    lines.append(f"来源群：{pending.get('group_id') or '未知'}")
    lines.append(f"提交人：{pending.get('submitted_by') or '未知'}")
    if pending.get("duplicate_note"):
        lines.append(f"⚠ 查重提示：{pending['duplicate_note']}")
    else:
        lines.append("查重：未发现名称+届次+比赛时间完全一致的记录")
    lines.append("")
    lines.append(f"回复「同意 {pending['id']}」入库并通知各群，或「拒绝 {pending['id']}」丢弃。")
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
    """CDUTETC ACM 机器人：OneBot 协议接入普通 QQ 账号。"""

    def __init__(self, client: OneBotClient | None = None):
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
        sched.add_job(
            self._remind_job,
            "interval",
            minutes=config.REMIND_CHECK_INTERVAL_MINUTES,
            id="remind-check",
            next_run_time=now,
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
                hours=config.REMIND_BEFORE_HOURS,
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
                admin_gid = admin_group_id()
                if not admin_gid:
                    # 引导场景：一个群都没配时，仍要允许超管用 /加群 把第一个群加进来，
                    # 否则会陷入“没群 → 不能用命令 → 永远加不了群”的死锁。
                    cmd, _ = parse_command(content)
                    if cmd not in _GROUP_FREE_COMMANDS:
                        await self.client.send_private_msg(
                            uid,
                            "未配置允许群，无法定位管理群（可用 /加群 <群号> 添加，"
                            "或设置 ACM_ADMIN_GROUP / ACM_ALLOWED_GROUPS）",
                        )
                        return
                replies = await commands.handle_command(content, admin_gid, uid)
                for reply in replies or []:
                    if reply.get("text"):
                        await self.client.send_private_msg(uid, reply["text"])
                return
            await self.client.send_private_msg(uid, f"收到（{uid}）")
        except Exception:
            logger.exception("私聊处理失败")

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
            await self.client.send_private_msg(uid, "当前没有待确认的发布申请。")
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
            await self.client.send_private_msg(uid, "\n".join(lines))
            return True
        try:
            if approve:
                cid, created = db.approve_pending_publication(pid, operator=uid)
                pending_item = db.get_pending_publication(pid) or {}
                source_group = pending_item.get("group_id") or ""
                if created:
                    _apply_publish_scope(cid, source_group)
                    await self.client.send_private_msg(
                        uid, f"已入库：#{cid} {pending_item.get('name') or ''}，正在通知本群。"
                    )
                    await self._notify_publish_group(pending_item, cid)
                else:
                    # 已存在同场比赛：把来源群并入通知范围，不再新建
                    extended = _extend_scope_with_group(cid, source_group)
                    hint = "已把该群加入通知范围。" if extended else "该群已在通知范围内。"
                    await self.client.send_private_msg(
                        uid, f"该比赛已存在（编号 {cid}），未重复入库。{hint}"
                    )
            else:
                if not db.reject_pending_publication(pid, operator=uid, note=note):
                    await self.client.send_private_msg(uid, f"申请 #{pid} 已处理过，无需重复操作。")
                    return True
                await self.client.send_private_msg(uid, f"已拒绝申请 #{pid}，未入库。")
        except ValueError as error:
            await self.client.send_private_msg(uid, f"处理失败：{error}")
        return True

    async def _notify_publish_group(self, pending_item: dict, cid: int) -> None:
        """超管同意后发送新比赛通知：只发给该比赛的通知范围（默认仅来源群）。"""
        info = json.loads(pending_item.get("fields_json") or "{}")
        notice = _build_publish_notice(info, cid)
        try:
            scope = db.get_competition_notify_groups(cid)
        except (sqlite3.Error, ValueError):
            scope = set()
        targets = sorted(scope & config.ALLOWED_GROUPS) if scope else sorted(config.ALLOWED_GROUPS)
        sent = 0
        for gid in targets:
            if await self._send_group(gid, notice):
                sent += 1
        if sent < len(targets):
            logger.warning("比赛 #%s 群通知发送 %s/%s", cid, sent, len(targets))

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
            targets = mentioned_user_ids(message.content)
            # self_id 是当前连接账号的真实 QQ 号，最可信；ACM_BOT_QQ 只在
            # 事件没带 self_id 时兜底。顺序反了会导致配错 BOT_QQ 后
            # 所有 @ 都不被识别、普通提问被静默丢弃。
            self_id = str(message.raw.get("self_id") or "")
            bot_qq = self_id or config.BOT_QQ
            addressed = bool(bot_qq) and bot_qq in targets
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
                    or is_publish_command(text, config.PUBLISH_CMD)
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
            if is_publish_command(text, config.PUBLISH_CMD):
                await self._handle_publish(message, text, group_id, member_id)
                return
            replies = await commands.handle_command(text, group_id, member_id)
            if replies is None:
                # 非命令消息进入问答链路前，先过五层防护入口闸门
                allowed, gate_reply = self._qa_gate(member_id, text)
                if not allowed:
                    if gate_reply:
                        await self._safe_reply(message, gate_reply)
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
            for reply in replies:
                if not reply.get("text"):
                    continue
                target_group = reply.get("group_id", group_id)
                if target_group == group_id:
                    await self._safe_reply(message, reply["text"])
                else:
                    cross_total += 1
                    if await self._send_group(target_group, reply["text"]):
                        cross_sent += 1
            if cross_total:
                await self._safe_reply(message, _notification_summary(cross_sent, cross_total))
        except Exception:
            logger.exception("处理群消息失败")
            await self._safe_reply(message, "出错了，稍后再试试~")
        finally:
            _reply_sequence.reset(reply_token)

    # ---------- 内部处理 ----------

    def _extract_member_id(self, message: OneBotMessage) -> str:
        """提取消息成员标识。

        OneBot 群消息直接取 user_id（QQ 号）；同时兼容旧 botpy 的
        author.member_openid / 顶层 member_openid / author_id 等字段，
        以便历史消息与测试消息都能稳定识别身份。返回统一字符串。
        """
        for attr in ("user_id", "member_openid", "author_id"):
            value = getattr(message, attr, None)
            if value not in (None, ""):
                return str(value)
        author = getattr(message, "author", None)
        if author is not None:
            for attr in ("member_openid", "user_id", "author_id"):
                value = getattr(author, attr, None)
                if value not in (None, ""):
                    return str(value)
        return ""

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

        url = extract_url(text)
        if not url:
            await self._safe_reply(
                message, "请带上比赛链接，例如：发布 https://example.com/contest"
            )
            return

        try:
            page = await fetch_text(url)
        except FetchError as e:
            await self._safe_reply(
                message,
                f"链接抓取失败：{e}。可以换个可访问的链接，或直接把比赛信息发给我~",
            )
            return

        info, err = await extract_contest(f"链接正文：{page}\n老师原文补充：{text}")
        if err is not None or not info:
            await self._safe_reply(
                message,
                f"信息提取失败：{err or '未知原因'}。"
                "可以直接告诉我：比赛名称、报名截止/比赛时间（YYYY-MM-DD HH:MM）、形式、地点~",
            )
            return

        info["source_link"] = url
        try:
            competition_info = map_competition_info(info)
        except ValueError as e:
            await self._safe_reply(message, f"信息校验失败：{e}")
            return

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
        if config.PUBLISH_CONFIRM_ENABLED and config.SUPER_ADMIN:
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
            for admin in sorted(config.SUPER_ADMIN):
                if await self.client.send_private_msg(admin, review):
                    sent += 1
            if sent:
                await self._safe_reply(
                    message,
                    f"已整理完毕并提交超管确认（申请 #{pid}）。"
                    "超管同意后会自动入库并通知各群。",
                )
            else:
                await self._safe_reply(
                    message,
                    f"已整理为申请 #{pid}，但超管私聊推送失败，请提醒超管查看待确认列表。",
                )
            logger.info("发布申请 #%s 已提交超管确认：%s", pid, info.get("name"))
            return

        # 未启用确认流程（或未配置超管）：保持原免审发布
        try:
            cid, created = create_published_competition_once(
                competition_info,
                source_url=url,
                source_text=page,
                operator=f"publish:{member_id}",
            )
        except ValueError as e:
            await self._safe_reply(message, f"信息校验失败：{e}")
            return

        if not created:
            await self._safe_reply(message, f"该比赛已存档（编号 {cid}），本群已在通知范围内。")
            return

        # 新比赛默认只通知发布它的群
        _apply_publish_scope(cid, group_id)
        logger.info("已免审发布比赛: %s（群 %s）", info.get("name"), group_id)
        display_info = {
            **info,
            "reg_deadline": competition_info.get("official_reg_deadline", ""),
            "contest_time": competition_info.get("official_time", ""),
        }
        await self._safe_reply(message, _build_confirm(display_info) + "\n已免审发布为正式比赛。")
        notice = _build_publish_notice(competition_info, cid)
        # 只发该比赛的通知范围（默认即发布它的群），不再全群广播
        try:
            scope = db.get_competition_notify_groups(cid)
        except (sqlite3.Error, ValueError):
            scope = set()
        targets = sorted(scope & config.ALLOWED_GROUPS) if scope else sorted(config.ALLOWED_GROUPS)
        sent = 0
        for gid in targets:
            if await self._send_group(gid, notice):
                sent += 1
        await self._safe_reply(message, _notification_summary(sent, len(targets)))

    async def _safe_reply(self, message: OneBotMessage, content: str) -> bool:
        """群内被动回复：普通 QQ 无平台 5 次上限，直接发回原群。"""
        group_id = message.group_id
        if not is_allowed_group(group_id, config.ALLOWED_GROUPS):
            return False
        return await self.client.send_group_msg(group_id, content)

    async def _notice_unauthorized_group(self, message: OneBotMessage,
                                         group_id: str) -> None:
        """未授权群被明确喊话时，回一句带群号的提示，方便管理员取号后授权。

        只在「@ 了机器人」或「发 /命令」时回复，避免被拉进群后对每条闲聊都插话；
        同一群有冷却时间，防止刷屏。提示本身不提供任何业务功能。
        """
        if not config.NOTICE_UNAUTHORIZED_GROUP:
            return
        raw = message.content or ""
        targets = mentioned_user_ids(raw)
        bot_qq = config.BOT_QQ or str(message.raw.get("self_id") or "")
        addressed = bool(bot_qq) and bot_qq in targets
        text = strip_at(raw)
        if not (addressed or text.startswith("/")):
            return
        now = time.monotonic()
        if now - _unauthorized_notice_at.get(group_id, 0.0) < config.NOTICE_UNAUTHORIZED_INTERVAL:
            return
        _unauthorized_notice_at[group_id] = now
        try:
            await self.client.send_group_msg(
                group_id,
                f"本群尚未授权（群号 {group_id}）。\n"
                f"请管理员在已授权群或私聊机器人发送：/加群 {group_id}",
            )
        except Exception:  # noqa: BLE001
            logger.warning("未授权群提示发送失败: %s", group_id)

    # ---------- 定时任务回调 ----------

    async def _send_group(self, group_id: str, text: str) -> bool:
        """向允许群发送主动文本消息。"""
        if not is_allowed_group(group_id, config.ALLOWED_GROUPS) or not text:
            return False
        if not config.GROUP_ACTIVE_MESSAGES_ENABLED:
            logger.info("主动群消息未启用，跳过群 %s 的通知", group_id)
            return False
        return await self.client.send_group_msg(group_id, text)

    async def _send_group_log_only(self, group_id: str, text: str) -> bool:
        """兼容旧回调名；发送行为由主动消息开关控制。"""
        return await self._send_group(group_id, text)

    async def _remind_job(self):
        """周期检查提醒：比赛临近 + 征集登记截止 + 训练开始。"""
        if not config.GROUP_ACTIVE_MESSAGES_ENABLED:
            logger.info("主动群消息未启用，暂停提醒投递")
            return
        await check_reminders(self._send_group)
        await check_general_reminders(self._send_group)

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
        # 周赛通知范围完全由配置决定：每次运行都对齐（含清空），
        # 这样改 ACM_WEEKLY_CONTEST_GROUPS 后对本周已生成的记录也立即生效。
        # 注意不要与 ALLOWED_GROUPS 求交后写回：若交集为空会变成「空=全部群」，
        # 反而把周赛推给所有群。这里按配置原样写入，取不到有效群时宁可不提醒。
        desired = set(config.WEEKLY_CONTEST_GROUPS)
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
        """周期抓取公共来源（默认关闭）：变化检测 → 免审记录 → 不通知全员。"""
        await crawler.run_fetch(send_group=self._send_group)


def run_bot() -> None:
    """启动入口：配置检查 → 创建 OneBot 客户端 → 常驻连接。"""
    if config.ENV_CREATED:
        logger.warning(
            "未找到 .env，已按 .env.example 生成：%s\n"
            "请填写 ACM_ONEBOT_WS_URL、ACM_LLM_API_KEY、ACM_ALLOWED_GROUPS、"
            "ACM_SUPER_ADMIN、ACM_TEACHER_WHITELIST 后重新启动。",
            config.ENV_FILE,
        )
    config.check_config()
    empty = config.describe_empty_config()
    if empty:
        logger.warning("以下配置留空，对应功能不会生效: %s", ", ".join(empty))
    client = OneBotClient(
        ws_url=config.ONEBOT_WS_URL,
        token=config.ONEBOT_TOKEN,
    )
    ACMBot(client)
    try:
        asyncio.run(client.start())
    except KeyboardInterrupt:
        logger.info("收到中断信号，机器人退出")


if __name__ == "__main__":
    run_bot()
