"""事件入口 Mixin：客户端就绪、私聊消息、群 @ 消息的分发与权限校验。"""

from __future__ import annotations

import logging

from cdutetc import main as _main
from cdutetc import publish_format as _fmt
from cdutetc.bot import jobs as _jobs
from cdutetc.bot import replies as _replies

logger = logging.getLogger(__name__)


# 这些命令不依赖具体群，未配置任何允许群时也要放行（否则无法引导加第一个群）
_GROUP_FREE_COMMANDS = frozenset(
    {"加群", "删群", "群列表", "加老师", "删老师", "老师列表", "帮助", "help"}
)


class EventsMixin:
    """按职责拆分的 ACMBot 能力 Mixin。"""

    async def _on_ready(self, now: _main.datetime | None = None):
        """连接成功后补未来周赛并立即检查提醒，再开始周期任务（幂等）。"""
        if self.sched is not None:
            logger.info("机器人已上线（调度器已存在，跳过重复创建）")
            return
        _main.init_db()
        # 合并 .env 基线与数据库里的动态白名单（超管 /加群 /加老师 添加的条目）
        _main.roles.refresh()
        tz = _main.ZoneInfo(_main.config.TZ_NAME)
        now = now or _main.datetime.now(tz)
        now = now.replace(tzinfo=tz) if now.tzinfo is None else now.astimezone(tz)
        sched = _main.AsyncIOScheduler(timezone=_main.config.TZ_NAME)
        # 通用与旧群赛事提醒共用扫描周期。
        sched.add_job(
            self._general_remind_job,
            "interval",
            minutes=_main.config.REMIND_CHECK_INTERVAL_MINUTES,
            id="general-remind-check",
            next_run_time=now,
            max_instances=1,
            coalesce=True,
        )
        sched.add_job(
            self._contest_remind_job,
            "interval",
            seconds=_main.config.CONTEST_REMIND_CHECK_SECONDS,
            id="contest-remind-check",
            next_run_time=now,
            max_instances=1,
            coalesce=True,
        )
        # 公共来源抓取改为每天固定时刻一次（默认 08:05，可用 ACM_CONTEST_CRAWL_TIME 覆盖）；
        # FETCH_MIN_INTERVAL_MINUTES 只约束手动 /抓取 的触发频率，不再驱动定时任务。
        sched.add_job(
            self._crawl_job,
            "cron",
            hour=_main.config.CONTEST_CRAWL_HOUR,
            minute=_main.config.CONTEST_CRAWL_MINUTE,
            id="source-crawl",
            max_instances=1,
            coalesce=True,
        )
        if _main.config.AUTO_CONTEST_CRAWL_ENABLED:
            # 外部赛程同步单独成 job：启动即同步一次（重启后不必干等到第二天），
            # 之后每天同一时刻再拉；否则机器人重启当天会一直缺赛程。
            sched.add_job(
                self._contest_crawl_job,
                "cron",
                hour=_main.config.CONTEST_CRAWL_HOUR,
                minute=_main.config.CONTEST_CRAWL_MINUTE,
                id="contest-crawl",
                next_run_time=now,
                max_instances=1,
                coalesce=True,
            )
        if _main.config.DAILY_DIGEST_ENABLED:
            # 每日固定时刻推送一次聚合晨报；coalesce 保证服务重启/休眠后只补一次。
            sched.add_job(
                self._daily_digest_job,
                "cron",
                hour=_main.config.DAILY_DIGEST_HOUR,
                minute=_main.config.DAILY_DIGEST_MINUTE,
                id="daily-digest",
                max_instances=1,
                coalesce=True,
            )
        if _main.config.WEEKLY_CONTEST_ENABLED:
            _, start, _ = _jobs._weekly_contest_window(now)
            # 提醒窗口打开前一个扫描周期预生成，避免开赛时才生成及同刻调度竞争。
            generation_at = start - _main.timedelta(
                hours=max(_main.config.REMIND_BEFORE_HOURS, *_main.config.REMIND_OFFSET_HOURS,
                          max(_main.config.CONTEST_REMIND_OFFSETS_MINUTES) / 60),
                minutes=_main.config.REMIND_CHECK_INTERVAL_MINUTES,
            )
            sched.add_job(
                self._weekly_contest_job,
                "cron",
                day_of_week=generation_at.weekday(),
                hour=generation_at.hour,
                minute=generation_at.minute,
                id="weekly-contest",
                max_instances=1,
                coalesce=True,
            )
            await self._weekly_contest_job(now=now)
        if _main.config.PERSONA_CHAT_ENABLED:
            # 每日三次生活状态机：12:30 / 18:30 / 23:00（Asia/Shanghai）各切一句，
            # 零 Token（内置语料），打断/重启后 coalesce 只补一次，不重复刷库。
            for hour, minute in _main.persona_chat.DAILY_STATE_SLOTS:
                sched.add_job(
                    self._persona_state_job,
                    "cron",
                    hour=hour,
                    minute=minute,
                    id=f"persona-daily-state-{hour:02d}{minute:02d}",
                    max_instances=1,
                    coalesce=True,
                )
        sched.add_job(
            self._group_cleanup_job,
            "cron",
            hour=4,
            minute=10,
            timezone="Asia/Shanghai",
            id="group-cleanup",
            max_instances=1,
            coalesce=True,
        )
        sched.start()
        self.sched = sched
        logger.info(
            "机器人已上线：每 %d 分钟检查提醒，每天 %02d:%02d 抓取一次公共来源%s",
            _main.config.REMIND_CHECK_INTERVAL_MINUTES,
            _main.config.CONTEST_CRAWL_HOUR,
            _main.config.CONTEST_CRAWL_MINUTE,
            "，自动周赛已开启（%s %s-%s）" % (
                "周日" if _main.config.WEEKLY_CONTEST_WEEKDAY == 6 else f"星期{_main.config.WEEKLY_CONTEST_WEEKDAY + 1}",
                _main.config.WEEKLY_CONTEST_START,
                _main.config.WEEKLY_CONTEST_END,
            ) if _main.config.WEEKLY_CONTEST_ENABLED else "，自动周赛关闭",
        )

    async def on_private_message_create(self, message: _main.QQBotMessage):
        """私聊消息：超管可发 /命令 私聊管理（FAQ/课程/校安排等），其余回执验证链路。
        默认不记录私聊原文；初次部署采集白名单时设置 ACM_DEBUG_LOG_IDENTITY=1 临时开启。"""
        token = _replies._private_reply_context.set((str(message.user_id), str(getattr(message, "id", "") or ""), 0))
        try:
            uid = message.user_id
            content = message.content or ""
            if not _main.is_super_admin(uid):
                # 老师在群里发队规后，草稿确认回到私聊：回「确认 12」/「FAQ驳回 12」。
                # 必须放在超管闸门之前——老师不是超管，放后面就永远收不到自己的回执。
                decision = (_main.commands.handle_pending_decision(str(uid), content)
                            if _main.can_manage_group(uid) else None)
                if decision:
                    for line in decision:
                        await self._send_private_result(uid, line)
                    return
            # 私聊社交独立于管理工作区；简单称呼零 Token，队务/命令继续走权限门禁。
            if not _main.is_super_admin(uid) and await self._handle_private_chat(message, content, str(uid)):
                return
            # 私聊作用域闸门：只有超管能进管理链路，老师与普通成员一律在这里返回，
            # 避免老师借私聊拿到跨群配置/比赛，也避免后续审批与命令被误触发。
            if not _main.is_super_admin(uid):
                if content.strip().startswith("/"):
                    await self._send_private_result(
                        uid, "⛔ 私聊管理仅对超级管理员开放，请在已授权群内使用命令")
                else:
                    await self._send_private_result(
                        uid, "你好～群内问题请在群里 @我，私聊管理仅向超管开放。")
                return
            if _main.config.DEBUG_LOG_IDENTITY:
                logger.info("[私聊][调试] user_id=%s content=%s", uid, content[:50])
            # 超管私聊回复「同意/通过/拒绝 <编号>」处理待确认的发布申请
            if _main.is_super_admin(uid):
                handled = await self._handle_publish_decision(uid, content)
                if handled:
                    return
                # 超管也可代为确认老师的 FAQ 草稿（allow_any），回执同样走私聊。
                decision = _main.commands.handle_pending_decision(str(uid), content, allow_any=True)
                if decision:
                    for line in decision:
                        await self._send_private_result(uid, line)
                    return
            if await self._handle_private_chat(message, content, str(uid)):
                return
            if content.strip().startswith("/") and _main.is_super_admin(uid):
                cmd, args = _main.parse_command(content)
                from cdutetc import group_admin
                # 短别名归一：/面板→/管理、/绑老师→/群老师、/改→/修正
                cmd = _main.commands.canonical_command(cmd)
                # 目标群会话指令最先处理：它们决定后续业务指令作用到哪个群
                if cmd in _main.commands.PRIVATE_SESSION_COMMANDS:
                    for reply in _main.commands.handle_session_command(cmd, args, str(uid)) or []:
                        if reply.get("text"):
                            await self._send_private_result(uid, reply["text"])
                    return
                if cmd in group_admin.COMMANDS:
                    # /群配置 <序号|群名> 等价于切换工作区：先落库，再直接展示该群面板，
                    # 这样不带 group_openid 也能一步完成「选群 + 看配置」。
                    tokens = (args or "").split()
                    if cmd == "群配置" and len(tokens) == 1 and tokens[0] != "全部":
                        if _main.commands.bind_active_group(str(uid), tokens[0]):
                            args = ""
                    # 已绑定目标群时，群配置类指令默认落到该群（不带参数的 /群配置 即查该群）；
                    # 显式 /群配置 全部 属于跨群查询，仍按全局视角处理。
                    bound = _main.admin_session.get_active_group(str(uid)) or ""
                    if "全部" in (args or ""):
                        bound = ""
                    replies = group_admin.handle_command(cmd, args, uid, bound, private=True)
                    if any(reply.get("refresh_weekly") for reply in replies or []):
                        await self._weekly_contest_job()
                    # 私聊管理回执同样带工作区标牌，与业务指令出口保持一致
                    for reply in _main.commands.with_private_header(replies, str(uid)) or []:
                        await self._send_private_result(uid, reply["text"])
                    return
                if cmd == "发布":
                    # 超管私聊录入比赛：作用域=当前工作区群，抓取/提取/查重/入库全部复用
                    # 群内 _handle_publish 链路，只是回执改走私聊，不在目标群刷屏。
                    pub_group = _main.admin_session.get_active_group(str(uid))
                    if not pub_group:
                        await self._send_private_result(
                            uid,
                            "还没有锁定工作区群，无法确定发布到哪个群。\n"
                            "请先发送 /群配置 <序号> 选中目标群（/群配置 全部 可查看候选），"
                            "再发送 /发布。",
                        )
                        return
                    # 保留多行正文与链接：从原文里剥掉前缀命令词，不做任何摘要
                    pub_text = content.strip()[3:].strip()
                    if not pub_text:
                        await self._send_private_result(
                            uid,
                            "用法：/发布 比赛名称 比赛时间（可附官网链接，支持多行）\n"
                            f"本次将录入到工作区群 {pub_group}，所有回执只在当前私聊返回。",
                        )
                        return
                    # 轻量消息对象：_handle_publish 只用到 group_id / id，
                    # 另带 _private_uid 标记让 _safe_reply 改走私聊回执。
                    priv_msg = _main.types.SimpleNamespace(
                        group_id=pub_group,
                        user_id=uid,
                        id=str(getattr(message, "id", "") or ""),
                        _private_uid=uid,
                    )
                    await self._handle_publish(priv_msg, pub_text, pub_group, str(uid))
                    return
                if cmd == "通知测试":
                    await self._send_private_result(uid, await self._notification_test(uid, args))
                    return
                if cmd in _main.commands.CLEANUP_COMMANDS:
                    # 清理仅用真实选群会话；未选群时直接给引导，不落到 ADMIN_GROUP。
                    replies = await _main.commands.handle_command(content, "", str(uid), private=True)
                    for reply in replies or []:
                        if reply.get("text"):
                            await self._send_private_result(uid, reply["text"])
                    return
                # 私聊绑定目标群后，业务指令按该群的单群视角执行（写库/查询/诊断都落到该群），
                # 管理回执走私聊；比赛修正产生的公告按目标群主动投递。
                active_group = _main.admin_session.get_active_group(str(uid))
                admin_gid = active_group or _main.admin_group_id()
                if not admin_gid:
                    # 引导场景：一个群都没配时，仍要允许超管用 /加群 把第一个群加进来，
                    # 否则会陷入“没群 → 不能用命令 → 永远加不了群”的死锁。
                    cmd, _ = _main.parse_command(content)
                    if cmd not in _GROUP_FREE_COMMANDS:
                        await self._send_private_result(
                            uid,
                            "未配置允许群，无法定位管理群（可用 /加群 <群号> 添加，"
                            "或设置 ACM_ADMIN_GROUP / ACM_ALLOWED_GROUPS）",
                        )
                        return
                # 私聊修正没有来源群；所有目标群都必须收到主动通知。
                command_group = "" if cmd == "修正" else admin_gid
                replies = await _main.commands.handle_command(
                    content, command_group, uid, private=True, active_group=active_group)
                group_notices = []
                for reply in replies or []:
                    if reply.get("text"):
                        if reply.get("active_group"):
                            group_notices.append(reply)
                        else:
                            await self._send_private_result(uid, reply["text"])
                if group_notices:
                    results = await self._send_group_notices(group_notices)
                    await self._send_private_result(uid, _fmt._notification_details(results))
                return
            from cdutetc import group_admin
            if _main.is_super_admin(uid):
                await self._send_private_result(uid, group_admin.answer_private(content, uid))
            else:
                await self._send_private_result(uid, "你好～群内问题请在群里 @我，私聊管理仅向超管开放。")
        except Exception:
            logger.exception("私聊处理失败 [%s]", _main.AppErrorCode.INTERNAL)
        finally:
            _replies._private_reply_context.reset(token)

    async def on_group_at_message_create(self, message: _main.QQBotMessage):
        reply_token = _replies._reply_sequence.set(0)
        try:
            group_id = message.group_id
            # 默认不记录群号/QQ号/消息内容，避免长期留存个人信息；
            # 初次部署采集白名单时设置 ACM_DEBUG_LOG_IDENTITY=1 临时开启。
            if _main.config.DEBUG_LOG_IDENTITY:
                logger.info(
                    "[身份采集][调试] group_id=%s user_id=%s content=%s",
                    group_id,
                    getattr(message, "user_id", "") or "",
                    (message.content or "")[:50],
                )
            if not _main.is_allowed_group(group_id, _main.config.ALLOWED_GROUPS):
                text = _main.strip_at(message.content or "")
                cmd, _ = _main.parse_command(text)
                member_id = self._extract_member_id(message)
                addressed = _main.is_addressed_to_bot(message)
                targets = {item["id"] for item in _main.message_mentions(message) if item["id"]}
                if (cmd == "认领此群" and _main.is_super_admin(member_id)
                        and (addressed or (not _main.config.GROUP_REQUIRE_AT and not targets))):
                    replies = await _main.commands.handle_command(text, group_id, member_id)
                    for reply in replies or []:
                        if reply.get("text"):
                            if self.supports_passive_reply and not getattr(message, "id", ""):
                                continue
                            seq = _replies._clamp_reply_seq(_replies._reply_sequence.get() + 1)
                            _replies._reply_sequence.set(seq)
                            await _main.notifications.send_group(
                                self.client, group_id, reply["text"],
                                reply_to=str(getattr(message, "id", "") or ""), reply_seq=seq,
                            )
                    return
                logger.info("非授权群消息已忽略: %s", group_id)
                await self._notice_unauthorized_group(message, group_id)
                return
            addressed = _main.is_addressed_to_bot(message)
            targets = {item["id"] for item in _main.message_mentions(message) if item["id"]}
            text = _main.strip_at(message.content or "")
            teacher_rate_checked = False
            from cdutetc import business_followup

            # 老师通知面向学生/全体也可记安排；他人的问句与命令仍在下面忽略。
            if (targets and not addressed and not text.startswith("/")
                    and _main.match_publish_command(text, _main.config.PUBLISH_CMD) is None
                    and not business_followup.is_location_message(text)
                    and _main.teacher_intent.should_call_llm(text)
                    and await self._handle_teacher_intent(
                        message, text, group_id, self._extract_member_id(message),
                        fallback_on_ignore=True)):
                return True
            # 带 @ 的消息只有指向本机器人时才受理，包括发给他人的命令。
            if targets and not addressed:
                logger.info(
                    "群消息 @ 的不是本机器人，已忽略（@目标=%s）",
                    ",".join(sorted(targets)),
                )
                return
            # 收图与文本滑窗在「有没有正文」之前处理：纯图片消息没有文本，
            # 但它正是要收的对象；后台任务异步跑，绝不阻塞本条消息的响应。
            self._schedule_meme_collect(message, group_id, text)
            if not text:
                if not addressed or (getattr(message, "raw", {}) or {}).get("attachments"):
                    return
                # 真实平台 @ 本身就是喊话，空正文也可本地回一句；纯图片仍交收图。
                # 称呼取人设单一事实源，改名字只改 assets/persona.txt。
                text = _main.persona_chat.bot_names()[0]
            from cdutetc import private_chat

            # 正文喊名触发人设聊天：NAME_ADDRESS_ENABLED 开启时，正文（已 strip_at）
            # 以任一识别名开头即视为喊话，仅放开 local_persona_reply 这一处入口。
            # 局部 import 避开循环依赖；不触碰 is_addressed_to_bot，故地点问句 /
            # 定时播报 / 问答主流程 / 发布命令等依赖 addressed 的门禁保持原样。
            from cdutetc import persona_chat
            import re

            name_addressed = (
                _main.config.NAME_ADDRESS_ENABLED
                and bool(re.match(persona_chat.bot_name_regex(), (text or "").strip()))
            )
            local_social = private_chat.local_persona_reply(text)
            if (addressed or name_addressed) and local_social:
                member_id = self._extract_member_id(message)
                allowed, gate_reply = self._qa_gate(member_id, text)
                if not allowed:
                    if gate_reply:
                        await self._safe_reply(message, gate_reply)
                    return
                if (_main.config.QA_SOCIAL_CHAT_ENABLED
                        and _main.db.group_switch_enabled(group_id, "qa_enabled")):
                    llm_replied = False
                    # 喊名（纯文本喊名或真 @ 后正文为喊名）命中固定短句时，先尝试一次
                    # 均衡省 Token 的人设 LLM 回复；仅当人设聊天开关与 API Key 齐备才尝试。
                    # 任何失败、无 Key、异常或空输出都一字不改回退下面的本地固定句。
                    # 限流已在上游 _qa_gate 扣过，_handle_persona_chat 内部重查不重复扣。
                    if (name_addressed
                            and _main.config.PERSONA_CHAT_ENABLED
                            and _main.config.LLM_API_KEY):
                        try:
                            llm_replied = await self._handle_persona_chat(
                                message, text, group_id, member_id,
                                lightweight=True, max_sentences=2)
                        except Exception:
                            logger.warning("纯文本喊名接 LLM 失败，回退本地固定句",
                                           exc_info=True)
                            llm_replied = False
                    if not llm_replied:
                        await self._safe_reply(message, _main.content_filter.sanitize_output(
                            local_social, max_length=_main.config.QA_OUTPUT_MAX_LENGTH,
                            max_link_length=_main.config.QA_OUTPUT_MAX_LINK_LENGTH,
                        ))
                return
            # 显式地点补充在老师自动意图与聊天前处理；无@也仅放行有本群写权限的老师。
            from cdutetc import business_followup, roles

            # 地点问句是只读查询，先于补地点处理；无@时也必须阻断老师写意图。
            if not text.startswith("/") and business_followup.is_location_query(text):
                if not addressed:
                    return
                member_id = self._extract_member_id(message)
                allowed, gate_reply = self._qa_gate(member_id, text)
                if not allowed:
                    if gate_reply:
                        await self._safe_reply(message, _main.content_filter.sanitize_output(
                            gate_reply, max_length=_main.config.QA_OUTPUT_MAX_LENGTH,
                            max_link_length=_main.config.QA_OUTPUT_MAX_LINK_LENGTH,
                        ))
                    return
                if not _main.db.group_switch_enabled(group_id, "qa_enabled"):
                    return
                from cdutetc.handlers import faq_cmd

                answer = await faq_cmd._answer_from_knowledge_base(text, group_id)
                if answer:
                    if not any(line.startswith("地点：") for line in answer.splitlines()):
                        answer += "\n地点：还没登记，暂时无法确定"
                else:
                    answer = "本群暂无对应的已登记地点，请明确课程或比赛名称。"
                await self._safe_reply(message, _main.content_filter.sanitize_output(
                    answer, max_length=_main.config.QA_OUTPUT_MAX_LENGTH,
                    max_link_length=_main.config.QA_OUTPUT_MAX_LINK_LENGTH,
                ))
                return
            if (not text.startswith("/") and business_followup.is_location_message(text)
                    and (addressed or (roles.resolve_admin_scope(self._extract_member_id(message), group_id)
                                      and _main.config.TEACHER_AUTO_INTENT_ENABLED))):
                result = await _main.asyncio.to_thread(
                    business_followup.handle_location, text, group_id, self._extract_member_id(message),
                    msg_id=str(getattr(message, "id", "") or ""),
                    reference_id=str(getattr(message, "reference_id", "") or ""),
                )
                if result is not None:
                    await self._safe_reply(message, result["reply"])
                    return
            # 群全量消息入口：老师没 @ 机器人、也没带 / 命令，只是在群里说安排
            # （「周三晚七点讲图论」）时，交给隐式意图管线判断要不要登记。
            # 队员的普通消息在这里一定返回 False，继续走原有逻辑直至放行。
            natural_publication = (addressed and _main.config.NL_INTENT_ENABLED
                                   and _main.is_timed_contest_announcement(text))
            if (addressed and not text.startswith("/")
                    and not natural_publication
                    and _main.match_publish_command(text, _main.config.PUBLISH_CMD) is None
                    and _main.config.TEACHER_AUTO_INTENT_ENABLED
                    and _main.teacher_intent.is_teacher_speaker(group_id, self._extract_member_id(message))
                    and _main.teacher_intent.should_call_llm(text)):
                teacher_rate_checked = True
                allowed, gate_reply = self._qa_gate(self._extract_member_id(message), text)
                if not allowed:
                    if gate_reply:
                        await self._safe_reply(message, gate_reply)
                    return
            if (not text.startswith("/")
                    and not natural_publication
                    and _main.match_publish_command(text, _main.config.PUBLISH_CMD) is None
                    and (not addressed or self._qa_gate(
                        self._extract_member_id(message), text, check_rate_limit=False)[0])
                    and await self._handle_teacher_intent(
                        message, text, group_id, self._extract_member_id(message),
                        fallback_on_ignore=addressed)):
                return True
            # 零 Token 群聊娱乐：跟风复读（+1）与高频词表情包，都只走被动回复，
            # 不调模型、不占主动配额；与上一步互斥，被意图管线消费的消息不会到这里。
            if (not addressed
                    and not text.startswith("/")
                    and _main.match_publish_command(text, _main.config.PUBLISH_CMD) is None
                    and await self._handle_chat_fun(
                        message, text, group_id, self._extract_member_id(message))):
                return
            # 拟人化插话：普通队员的闲聊在「高潮」时才可能被接一句（2 分钟 ≥8 条、
            # 距上次插话 ≥20 分钟、15% 概率）；不满足就完全安静，照旧被忽略。
            if (not addressed
                    and not text.startswith("/")
                    and _main.match_publish_command(text, _main.config.PUBLISH_CMD) is None
                    and await self._maybe_persona_interject(message, text, group_id)):
                return
            # 群内默认必须 @ 本机器人 才响应（含命令），避免抢答他人命令；
            # ACM_GROUP_REQUIRE_AT=0 时回退为“无 @ 也受理显式命令”。
            if _main.config.GROUP_REQUIRE_AT:
                allowed_without_at = False
            else:
                allowed_without_at = (
                    text.startswith("/")
                    or _main.match_publish_command(text, _main.config.PUBLISH_CMD) is not None
                )
            if not addressed and not allowed_without_at:
                logger.info(
                    "未 @ 本机器人，已忽略（消息含 @ 段：%s，文本长度 %d）",
                    bool(_main.message_mentions(message)),
                    len(text),
                )
                return
            member_id = self._extract_member_id(message)
            publish_payload = _main.match_publish_command(text, _main.config.PUBLISH_CMD)
            if publish_payload is not None:
                await self._handle_publish(message, publish_payload, group_id, member_id)
                return
            cmd, args = _main.parse_command(text)
            if cmd == "通知测试":
                await self._safe_reply(message, await self._notification_test(member_id, args, message=message))
                return
            replies = await _main.commands.handle_command(text, group_id, member_id)
            if any(reply.get("refresh_weekly") for reply in replies or []):
                await self._weekly_contest_job()
            if replies is None:
                # 先限流和检查输入，再考虑模型意图识别，避免被拒绝的问题仍产生费用。
                allowed, gate_reply = self._qa_gate(member_id, text, check_rate_limit=not teacher_rate_checked)
                if not allowed:
                    if gate_reply:
                        await self._safe_reply(message, gate_reply)
                    return
                # 老师/超管用自然语言描述比赛（没带「发布」）时，先让模型判断意图，
                # 判为「在发布比赛」就直接走发布流程，不必死记命令格式。
                if (
                    _main.config.NL_INTENT_ENABLED
                    and not text.startswith("/")
                    and not _main.content_filter.is_social_chat(text)
                    and _main.can_manage_group(member_id)
                ):
                    intent = "add_contest" if _main.is_timed_contest_announcement(text) else await _main.llm.classify_intent(text)
                    if intent == "add_contest":
                        logger.info("识别为发布比赛意图，转发布流程")
                        await self._handle_publish(message, text, group_id, member_id)
                        return
                # 群开关闸门：问答关闭后不再自动回答自由文本，命令仍照常执行
                if not _main.db.group_switch_enabled(group_id, "qa_enabled"):
                    logger.info("本群问答开关已关闭，跳过自动回答")
                    return
                # 寒暄先交给 persona：比固定话术更像人，也避免为一句「在吗」调两遍模型；
                # QA_SOCIAL_CHAT_ENABLED 关闭表示运维不想闲聊，此时不进 persona。
                if (_main.config.PERSONA_CHAT_ENABLED and _main.config.QA_SOCIAL_CHAT_ENABLED
                        and _main.content_filter.is_social_chat(text)
                        and await self._handle_persona_chat(
                            message, text, group_id, member_id)):
                    return
                replies = await _main.commands.handle_non_command_message(
                    text, group_id, member_id, staff=_main.can_manage_group(member_id)
                )
                # 拟人化闲聊兜底：非问句、FAQ 未命中时 commands 只会回一句「我在呢」，
                # 这类接梗更适合走 persona（更省 token 也更像人）。模型不可用时
                # _handle_persona_chat 返回 False，再由零 Token 本地兜底应一句，功能不退化。
                if (_main.config.PERSONA_CHAT_ENABLED and _main.config.QA_SOCIAL_CHAT_ENABLED
                        and _main.commands.is_social_fallback(replies)
                        and not _main.content_filter.is_information_request(text)):
                    if await self._handle_persona_chat(
                            message, text, group_id, member_id):
                        return
                    # @机器人 的闲聊必须应一句：模型没配/失败时用确定性本地话术兜底。
                    # local_generic_reply 自带业务/决策/敏感过滤，不会抢答队务。
                    if addressed:
                        generic = private_chat.local_generic_reply(text)
                        if generic:
                            replies = [{"group_id": group_id, "text": generic}]
                # 五层防护-5：输出过滤（FAQ/模型回答统一二次敏感检查、长度截断、禁超长链接）
                if replies:
                    replies = [
                        {**reply, "text": _main.content_filter.sanitize_output(
                            reply.get("text", ""),
                            max_length=_main.config.QA_OUTPUT_MAX_LENGTH,
                            max_link_length=_main.config.QA_OUTPUT_MAX_LINK_LENGTH,
                        )}
                        for reply in replies
                        if reply.get("text")
                    ]
            group_notices = []
            admin_notice_texts = []
            same_group_texts = [
                reply["text"] for reply in (replies or [])
                if reply.get("text") and reply.get("group_id", group_id) == group_id
            ]
            # 官方协议被动回复最多 5 条：超出的同群文本先合并进最后一帧再发，
            # 严禁直接发起第 6 条 _safe_reply（会被拒为 INVALID_REQUEST）。
            if self.supports_passive_reply:
                merged_texts = _replies._merge_group_reply_texts(same_group_texts)
                # 被动搭便车：只读查询的被动回复尾部追加一条最紧急待办，
                # 零消耗主动额度；单群冷却与内容提取都在命令层统一处理。
                if merged_texts and _main.config.DAILY_DIGEST_PIGGYBACK_ENABLED:
                    # 出错/无权限的回复不搭车：先把问题讲清楚，别混入广告式提示。
                    failed = any(reply.get("error_code") for reply in (replies or []))
                    if not failed and cmd in _main.commands.PIGGYBACK_COMMANDS:
                        merged_texts[-1] = _main.commands.append_piggyback_notice(
                            group_id, merged_texts[-1]
                        )
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
                    group_notices.append({"group_id": target_group, "text": reply["text"]})
                if reply.get("notify_super_admin"):
                    admin_notice_texts.append(reply["text"])
            if admin_notice_texts:
                notice = "管理变更确认：\n" + "\n".join(admin_notice_texts)
                sent = 0
                for admin in sorted(_main.config.SUPER_ADMIN):
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
                elif sent < len(_main.config.SUPER_ADMIN):
                    await self._safe_reply(
                        message,
                        f"操作已保存，超管私聊确认仅送达 {sent}/{len(_main.config.SUPER_ADMIN)} 人；"
                        "未收到者可查看 /群列表 或 /老师列表。",
                    )
            if group_notices:
                results = await self._send_group_notices(group_notices)
                await self._safe_reply(message, _fmt._notification_details(results))
        except Exception:
            logger.exception("处理群消息失败")
            await self._safe_reply(message, _main.safe_error_hint(_main.AppErrorCode.INTERNAL))
        finally:
            _replies._reply_sequence.reset(reply_token)
