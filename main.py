"""CDUTETC ACM 机器人主程序。

职责：连接 QQ 官方机器人，路由群消息：
  - 命令式操作（/修正、/征集、/报名、/征集汇总、/征集关闭 等）→ commands 模块分发
  - 「发布 <链接/信息>」命令（老师白名单）→ 抓取 → 提取 → 免审 confirmed → 群通知
  - 非命令消息 → FAQ 自动回复；未命中则收集为未答问题
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
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from onebot_client import OneBotClient, OneBotMessage

import commands
import config
import crawler
import database as db
from database import create_competition, create_published_competition, init_db
from fetcher import FetchError, fetch_text
from llm import extract_contest
from reminder import check_general_reminders, check_reminders
from utils import (
    extract_url,
    is_allowed_group,
    is_publish_command,
    is_teacher,
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


class ACMBot:
    """CDUTETC ACM 机器人：OneBot 协议接入普通 QQ 账号。"""

    def __init__(self, client: OneBotClient):
        self.client = client
        self.sched: AsyncIOScheduler | None = None
        client.group_msg_handler = self.on_group_at_message_create
        client.private_msg_handler = self.on_private_message_create
        client.on_ready = self._on_ready

    # ---------- 生命周期 ----------

    async def _on_ready(self):
        """连接成功后：初始化数据库、启动定时提醒与公共抓取（幂等）。"""
        if self.sched is not None:
            logger.info("机器人已上线（调度器已存在，跳过重复创建）")
            return
        init_db()
        self.sched = AsyncIOScheduler(timezone=config.TZ_NAME)
        self.sched.add_job(
            self._remind_job,
            "interval",
            minutes=config.REMIND_CHECK_INTERVAL_MINUTES,
            id="remind-check",
        )
        self.sched.add_job(
            self._crawl_job,
            "interval",
            minutes=config.FETCH_MIN_INTERVAL_MINUTES,
            id="source-crawl",
        )
        if config.WEEKLY_CONTEST_ENABLED:
            start_hh, start_mm = config.WEEKLY_CONTEST_START.split(":")
            self.sched.add_job(
                self._weekly_contest_job,
                "cron",
                day_of_week=config.WEEKLY_CONTEST_WEEKDAY,
                hour=int(start_hh),
                minute=int(start_mm),
                id="weekly-contest",
            )
        self.sched.start()
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
        """私聊消息：采集 QQ 号并回复，验证链路。"""
        try:
            uid = message.user_id
            content = message.content or ""
            logger.info("[私聊] user_id=%s content=%s", uid, content[:50])
            await self.client.send_private_msg(uid, f"收到（{uid}）")
        except Exception:
            logger.exception("私聊处理失败")

    # ---------- 群消息路由 ----------

    async def on_group_at_message_create(self, message: OneBotMessage):
        reply_token = _reply_sequence.set(0)
        try:
            group_id = message.group_id
            # 调试：采集 QQ 号用于配置白名单，确认后可删除本行
            logger.info(
                "[身份采集] group_id=%s user_id=%s content=%s",
                group_id,
                message.user_id,
                (message.content or "")[:50],
            )
            if not is_allowed_group(group_id, config.ALLOWED_GROUPS):
                logger.info("非授权群消息已忽略: %s", group_id)
                return
            text = strip_at(message.content)
            if not text:
                return
            member_id = self._extract_member_id(message)
            if is_publish_command(text, config.PUBLISH_CMD):
                await self._handle_publish(message, text, group_id, member_id)
                return
            replies = await commands.handle_command(text, group_id, member_id)
            if replies is None:
                replies = await commands.handle_non_command_message(text, group_id, member_id)
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
        """提取消息成员标识：OneBot 群消息直接取 user_id（QQ 号）。"""
        return message.user_id

    async def _handle_publish(self, message: OneBotMessage, text: str,
                              group_id: str, member_id: str):
        """发布命令：老师权限校验 → 抓取 → 提取 → 校验 → confirmed → 群通知。"""
        if not is_teacher(member_id):
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
            # 正式比赛、来源和已抓取正文基线一并提交，供后台检测后续变化。
            create_published_competition(
                {
                    "name": info.get("name", ""),
                    "official_reg_deadline": info.get("reg_deadline", ""),
                    "official_time": info.get("contest_time", ""),
                    "official_rules": info.get("raw_note", ""),
                    "official_site": info.get("source_link", ""),
                    "summary": "",
                },
                source_url=url,
                source_text=page,
                operator=f"publish:{member_id}",
            )
        except ValueError as e:
            await self._safe_reply(message, f"信息校验失败：{e}")
            return

        logger.info("已免审发布比赛: %s（群 %s）", info.get("name"), group_id)
        # 被动回复有有效期，先告知已提交存档，再等待各群通知结果。
        await self._safe_reply(message, _build_confirm(info) + "\n已免审发布为正式比赛。")
        notice = (
            f"【新比赛发布】{info.get('name') or '未知'}\n"
            f"报名截止：{info.get('reg_deadline') or '待定'}\n"
            f"比赛时间：{info.get('contest_time') or '待定'}\n"
            f"来源：{url}\n请及时查看并准备报名~"
        )
        targets = sorted(config.ALLOWED_GROUPS)
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
        """按北京时间本周的配置日期生成周赛（默认不通知全员）。

        数据库按周持久化去重，避免重启、重复调度或改名造成重复记录。
        now 供离线验证注入；无时区的注入时间按北京时间处理。
        """
        if not config.WEEKLY_CONTEST_ENABLED:
            return
        template = config.WEEKLY_CONTEST_TEMPLATE
        try:
            tz = ZoneInfo(config.TZ_NAME)
            now = now or datetime.now(tz)
            now = now.replace(tzinfo=tz) if now.tzinfo is None else now.astimezone(tz)
            if not 0 <= config.WEEKLY_CONTEST_WEEKDAY <= 6:
                raise ValueError("周赛星期必须在 0（周一）到 6（周日）之间")
            week_start = now.date() - timedelta(days=now.weekday())
            contest_date = week_start + timedelta(days=config.WEEKLY_CONTEST_WEEKDAY)
            start = datetime.combine(
                contest_date,
                datetime.strptime(config.WEEKLY_CONTEST_START, "%H:%M").time(),
                tzinfo=tz,
            )
            end = datetime.combine(
                contest_date,
                datetime.strptime(config.WEEKLY_CONTEST_END, "%H:%M").time(),
                tzinfo=tz,
            )
            if end <= start:
                raise ValueError("周赛结束时间必须晚于开始时间")
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
        if not created:
            logger.info("本周周赛记录已存在，跳过自动生成（比赛 id=%s）", cid)
            return
        logger.info("已自动生成周赛记录: %s（id=%s），未通知全员", template, cid)

    async def _crawl_job(self):
        """周期抓取公共来源（默认关闭）：变化检测 → 免审记录 → 不通知全员。"""
        await crawler.run_fetch(send_group=self._send_group)


def run_bot() -> None:
    """启动入口：配置检查 → 创建 OneBot 客户端 → 常驻连接。"""
    config.check_config()
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
