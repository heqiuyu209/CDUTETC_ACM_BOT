"""定时任务 Mixin：比赛提醒、通用提醒、每日摘要、周赛、爬虫巡检、周赛时间窗计算。"""

from __future__ import annotations

import logging
import asyncio

from cdutetc import main as _main

logger = logging.getLogger(__name__)


def _weekly_contest_window(now: _main.datetime):
    """返回下一场尚未开赛的周赛及其所属周；跨周生成不误建过去场次。"""
    tz = _main.ZoneInfo(_main.config.TZ_NAME)
    now = now.replace(tzinfo=tz) if now.tzinfo is None else now.astimezone(tz)
    if not 0 <= _main.config.WEEKLY_CONTEST_WEEKDAY <= 6:
        raise ValueError("周赛星期必须在 0（周一）到 6（周日）之间")
    week_start = now.date() - _main.timedelta(days=now.weekday())
    contest_date = week_start + _main.timedelta(days=_main.config.WEEKLY_CONTEST_WEEKDAY)
    start = _main.datetime.combine(
        contest_date, _main.datetime.strptime(_main.config.WEEKLY_CONTEST_START, "%H:%M").time(), tzinfo=tz,
    )
    end = _main.datetime.combine(
        contest_date, _main.datetime.strptime(_main.config.WEEKLY_CONTEST_END, "%H:%M").time(), tzinfo=tz,
    )
    if end <= start:
        raise ValueError("周赛结束时间必须晚于开始时间")
    if start <= now:
        week_start += _main.timedelta(days=7)
        start += _main.timedelta(days=7)
        end += _main.timedelta(days=7)
    return week_start, start, end


class JobsMixin:
    """按职责拆分的 ACMBot 能力 Mixin。"""

    async def _group_cleanup_job(self):
        """只执行已开启的本群清理规则；不生成聊天内容或发送 QQ 消息。"""
        try:
            from cdutetc import group_cleanup

            return await asyncio.to_thread(group_cleanup.run_rules)
        except Exception:
            logger.exception("每日本群清理规则执行失败")
            return {}

    async def _contest_remind_job(self):
        """仅发送已持久化的开赛节点，失败不影响旧版提醒和后续轮次。"""
        if not _main.config.GROUP_ACTIVE_MESSAGES_ENABLED:
            return
        try:
            await _main.check_contest_start_reminders(self._send_group_result)
        except Exception:
            logger.exception("开赛提醒计划扫描失败 [%s]", _main.AppErrorCode.INTERNAL)

    async def _general_remind_job(self):
        """周期检查征集、比赛和训练提醒。"""
        if not _main.config.GROUP_ACTIVE_MESSAGES_ENABLED:
            logger.info("主动群消息未启用，暂停提醒投递")
            return
        try:
            await _main.check_general_reminders(self._send_group_result)
            await _main.check_custom_reminders(self._send_group_result)
        except Exception:
            logger.exception("通用提醒扫描失败，等待下一轮重试")

    async def _daily_digest_job(self, now: _main.datetime | None = None) -> int:
        """每日聚合晨报：每群最多 1 条主动消息，汇总当日课程/征集/赛事。

        设计要点：
        - 只对授权群（运行时白名单）逐个生成，某群内容为空则整群跳过，
          不发空消息浪费额度；
        - 今日无比赛（且当天没有其它可报事务）时静默跳过，只记 INFO 日志，
          绝不发送「今日暂无比赛」这类空通知占用主动额度；
        - 单群失败不影响其他群：配额耗尽（11253/11263）已在
          ``_send_group_result`` 内持久熔断，这里只记告警并继续下一个群；
        - now 供离线验证注入，无时区按北京时间处理。
        """
        from cdutetc.errors import DeliveryCode

        if not _main.config.DAILY_DIGEST_ENABLED:
            return 0
        if not _main.config.GROUP_ACTIVE_MESSAGES_ENABLED:
            logger.info("主动群消息未启用，暂停晨报投递")
            return 0
        sent = 0
        skipped = 0
        try:
            for group_id in sorted(_main.config.ALLOWED_GROUPS):
                try:
                    if not _main.db.group_switch_enabled(group_id, "daily_digest_enabled"):
                        continue
                    detail = _main.daily_digest.collect_daily_digest(group_id, now=now)
                    text = detail["text"]
                    if not text:
                        # 硬性跳过规则：全天无赛即静默跳过；即便群开关关闭导致
                        # 赛事段为空，也不会发出任何「今日暂无比赛」字样。
                        if detail["contest_enabled"] and detail["today_contest_count"] == 0:
                            logger.info("[晨报] 群 %s 今日无比赛，已跳过发送", group_id)
                        else:
                            logger.info("[晨报] 群 %s 今日无可推送内容，已跳过发送", group_id)
                        skipped += 1
                        continue
                    result = await self._send_group_result(group_id, text)
                except Exception:
                    logger.exception("[晨报] 群 %s 生成或发送异常，继续下一个群", group_id)
                    continue
                if result.ok:
                    sent += 1
                    continue
                if result.code == DeliveryCode.QUOTA_EXHAUSTED:
                    logger.warning(
                        "[晨报] 群 %s 主动消息配额已耗尽，本群今日跳过（已熔断，不再重试）",
                        group_id,
                    )
                else:
                    logger.warning("[晨报] 群 %s 未发送 [%s]",
                                   group_id, getattr(result.code, "value", result.code))
        except Exception:
            logger.exception("[晨报] 本次调度执行异常，跳过本轮等待下次触发")
        if sent:
            logger.info("[晨报] 本次成功推送 %d 个群的事务总览", sent)
        if skipped:
            logger.info("[晨报] 本次跳过 %d 个无可推送内容的群", skipped)
        return sent

    async def _weekly_contest_job(self, now: _main.datetime | None = None):
        """提前生成下一场未来周赛（默认不通知全员）。

        数据库按周持久化去重，避免重启、重复调度或改名造成重复记录。
        now 供离线验证注入；无时区的注入时间按北京时间处理。
        """
        if not _main.config.WEEKLY_CONTEST_ENABLED:
            return
        try:
            await self._create_weekly_contest(now=now)
        except Exception:
            logger.exception("[周赛] 本轮调度执行异常，跳过本轮等待下次触发")

    async def _create_weekly_contest(self, now: _main.datetime | None = None) -> None:
        """按周幂等生成下一场周赛（由 ``_weekly_contest_job`` 统一兜底调用）。"""
        template = _main.config.WEEKLY_CONTEST_TEMPLATE
        try:
            now = now or _main.datetime.now(_main.ZoneInfo(_main.config.TZ_NAME))
            week_start, start, end = _weekly_contest_window(now)
            start_str = start.strftime("%Y-%m-%d %H:%M")
            end_str = end.strftime("%Y-%m-%d %H:%M")
            cid, created = _main.db.create_weekly_competition(
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
        desired = _main.db.get_weekly_groups()
        try:
            if _main.db.get_competition_notify_groups(cid) != desired:
                _main.db.set_competition_notify_groups(cid, desired, operator="weekly:auto")
        except (_main.sqlite3.Error, ValueError) as e:
            logger.warning("周赛通知群设置失败: %s", e)
        if not created:
            logger.info("该周周赛记录已存在，跳过自动生成（比赛 id=%s）", cid)
            return
        logger.info("已自动生成周赛记录: %s（id=%s），未通知全员", template, cid)

    async def _crawl_job(self):
        """周期抓取公共来源（默认关闭）：变化检测 → 待审更新，不自动广播。

        外部赛程抓取已拆到 ``_contest_crawl_job``，两者互不影响。
        """
        try:
            await _main.crawler.run_fetch(send_group=self._send_group_result)
        except Exception:
            logger.exception("[抓取] 本轮调度执行异常，跳过本轮等待下次触发")

    async def _contest_crawl_job(self):
        """抓取 CF / 牛客外部赛程：启动时与每日固定时刻各调一次。

        赛程抓取失败只记日志，任何异常都不逸出调度器。
        """
        if not _main.config.AUTO_CONTEST_CRAWL_ENABLED:
            return
        try:
            results = await _main.crawler.run_contest_fetch()
            created = sum(1 for item in results if item.get("kind") == "created")
            logger.info("[赛程抓取] 本轮处理 %s 条，新建 %s 条", len(results), created)
        except Exception:
            logger.exception("[赛程抓取] 本轮执行异常，跳过本轮等待下次触发")
