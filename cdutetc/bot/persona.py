"""人设与互动 Mixin：问答闸门、教师意图、闲聊与人设发言、表情包采集与发送。"""

from __future__ import annotations

import logging

from cdutetc import main as _main
from cdutetc.bot import replies as _replies

logger = logging.getLogger(__name__)


class PersonaMixin:
    """按职责拆分的 ACMBot 能力 Mixin。"""

    def _extract_member_id(self, message: _main.QQBotMessage) -> str:
        """适配层把平台成员 OpenID 规范到 user_id。"""
        return str(getattr(message, "user_id", "") or "")

    def _qa_gate(self, member_id: str, text: str, *,
                 check_rate_limit: bool = True) -> tuple[bool, str | None]:
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
        if _main.config.QA_BLACKLIST and member_id in _main.config.QA_BLACKLIST:
            logger.info("问答已忽略（黑名单成员）: %s", member_id)
            return False, None
        if (
            _main.config.QA_WHITELIST_ENABLED
            and _main.config.QA_WHITELIST
            and member_id not in _main.config.QA_WHITELIST
        ):
            logger.info("问答已忽略（非白名单成员）: %s", member_id)
            return False, _main.config.QA_WHITELIST_REPLY

        # 五层防护-2：敏感内容拦截（输入侧，llm 层另有双保险）
        if _main.config.QA_SENSITIVE_GATE_ENABLED and _main.content_filter.has_sensitive(text):
            logger.info("问答已拒绝（输入含敏感内容）: %s", member_id)
            return False, _main.config.QA_SENSITIVE_REPLY

        # 五层防护-4：频率防刷（单用户滑动窗口限流）
        if (check_rate_limit and _main.config.QA_RATE_LIMIT_ENABLED
                and not self._qa_rate_limiter.allow(member_id)):
            logger.info("问答已限流（成员 %s）", member_id)
            return False, _main.config.QA_RATE_LIMIT_REPLY

        return True, None

    @staticmethod
    def _apply_teacher_text_fallback(info: dict, text: str) -> dict:
        """老师原文兜底：官网正文吞掉时间/赛道时，用原文写明的日期与赛道补回。

        - 比赛时间：提取结果为空而原文明确给出日期时补全（不覆盖模型已提取的有效时间）；
        - 赛道：老师原文点名的赛道优先级最高，覆盖官网列出的全部赛项。
        """
        if not isinstance(info, dict):
            return info
        reference = _main.datetime.now(_main.ZoneInfo(_main.config.TZ_NAME))
        declared_time = _main.extract_declared_contest_time(text, now=reference)
        if declared_time and not (info.get("official_time") or info.get("contest_time")):
            info["official_time"] = declared_time
            info["contest_time"] = declared_time
            logger.info("官网正文未给出比赛时间，按老师原文补全: %s", declared_time)
        declared_tracks = _main.extract_declared_tracks(text)
        if declared_tracks and declared_tracks != (info.get("tracks") or ""):
            info["tracks"] = declared_tracks
            logger.info("赛道按老师原文覆盖: %s", declared_tracks)
        return info

    async def _handle_teacher_intent(self, message: _main.QQBotMessage, text: str,
                                     group_id: str, member_id: str, *,
                                     fallback_on_ignore: bool = False) -> bool:
        """老师在群里随口说安排时，识别意图并登记课程/比赛。

        Returns:
            True  → 本消息由意图管线接管（含判定为闲聊后的静默），调用方直接返回；
            False → 不是老师或与本管线无关，调用方按原有流程继续（队员闲聊不受影响）。
        """
        if not _main.config.TEACHER_AUTO_INTENT_ENABLED:
            return False
        try:
            result = await _main.teacher_intent.handle_message(
                text, group_id=group_id, sender_id=member_id,
                msg_id=str(getattr(message, "id", "") or ""),
            )
        except Exception:
            # 意图识别是锦上添花，任何异常都退回原有流程，不能拖垮群消息处理。
            logger.exception("老师意图识别失败，回退原有流程")
            return False
        if result is None:
            return False
        if fallback_on_ignore and result.get("action") not in {"saved", "pending"}:
            return False
        if result.get("action") in {"saved", "pending"}:
            # 写入已经完成；即使回执失败，后来的官方 AT 事件也不能重放副作用。
            message.business_consumed = True
        reply = result.get("reply") or ""
        if reply and result.get("action") in {"saved", "pending"}:
            await self._safe_reply(message, reply)
        for uid, body in result.get("notify_private") or []:
            if uid and body:
                await self._send_private_result(str(uid), body)
        return True

    async def _handle_chat_fun(self, message: _main.QQBotMessage, text: str,
                               group_id: str, member_id: str) -> bool:
        """群聊娱乐：跟风复读与高频词表情包，全程零 Token。

        Returns:
            True  → 本条消息已由娱乐模块回应（复读或表情包），调用方直接返回；
            False → 不回应，调用方继续原有流程（无 @ 时照旧被忽略）。
        """
        if not (_main.config.ECHO_ENABLED or _main.config.MEME_ENABLED):
            return False
        action = None
        if _main.config.ECHO_ENABLED:
            try:
                action = _main.chat_fun.get_engine().observe(group_id, text, member_id)
            except Exception:
                # 娱乐功能是锦上添花，判定出错只记录，不影响群消息主流程。
                logger.exception("复读机判定失败，跳过")
                action = None
        if action is not None:
            if action.kind == "echo":
                # 原样复读，不调模型、不拼接任何文案。
                sent = await self._safe_reply(message, action.text)
                if sent:
                    self._note_bot_spoke(group_id)
                return sent
            if action.kind == "interrupt":
                # 已进入打断分支：无论图有没有发出去，都不再叠加关键词表情包。
                if await self._send_meme(message, group_id, action.category):
                    self._note_bot_spoke(group_id)
                return True
        if not _main.config.MEME_ENABLED:
            return False
        try:
            # 场景化插图：连续问号 → question，饭点/深夜抱怨 → slacking。
            # 滑窗已由消息入口统一维护，这里不再重复 remember。
            scene = _main.memes.get_engine().scene_trigger(group_id, text, remember=False)
        except Exception:
            logger.exception("场景表情判定失败，跳过")
            scene = ""
        if scene:
            sent = await self._send_meme(message, group_id, scene, text=text,
                                         proactive=True)
            if sent:
                self._note_bot_spoke(group_id)
            return sent
        try:
            category = _main.memes.get_engine().should_trigger(group_id, text)
        except Exception:
            logger.exception("表情包触发判定失败，跳过")
            return False
        if not category:
            return False
        sent = await self._send_meme(message, group_id, category, text=text)
        if sent:
            self._note_bot_spoke(group_id)
        return sent

    def _note_bot_spoke(self, group_id: str) -> None:
        """告诉插话闸门「机器人刚说过话」，让复读/表情包/插话共享同一节奏。"""
        try:
            _main.persona_chat.get_gate().note_bot_spoke(group_id)
        except Exception:
            # 节奏登记失败只是可能多说一句，绝不能影响已发出的这条消息。
            logger.warning("插话节奏登记失败，忽略", exc_info=True)

    def _schedule_meme_collect(self, message: _main.QQBotMessage, group_id: str,
                               text: str) -> None:
        """按群开关把收图丢进后台任务；开关关闭时只维护文本滑窗。"""
        try:
            engine = _main.memes.get_engine()
            # 图片消息正文是 CQ 码串，先清干净再进滑窗，否则连问判定与归类全被污染。
            clean_text = _main.memes.strip_cq_codes(text)
            # 先取历史上下文，再记当前文本：归类要用的是「这张图前后在聊什么」。
            context = engine.recent_texts(group_id)[-_main.memes.COLLECT_CONTEXT_LINES:]
            engine.remember_text(group_id, clean_text)
            if not _main.is_allowed_group(group_id, _main.config.ALLOWED_GROUPS):
                return
            if not _main.db.meme_collect_enabled(group_id):
                return
            if not _main.memes.extract_image_refs(message):
                return
            task = _main.asyncio.create_task(
                _main.memes.collect_from_message(
                    message, group_id=group_id, text=clean_text, context=context,
                )
            )
        except Exception:
            logger.exception("收图任务投递失败，跳过")
            return
        self._meme_tasks.add(task)
        task.add_done_callback(self._meme_tasks.discard)

    async def _send_meme(self, message: _main.QQBotMessage, group_id: str,
                         category: str, *, text: str = "",
                         proactive: bool = False) -> bool:
        """通过官方富媒体上传和 file_info 缓存发送表情包。

        Args:
            text: 当前聊天文本，用于优先抽中文件名标签命中的那张图。
            proactive: 显式主动插图，共用主动通知的开关、熔断和成功回执。
        """
        from cdutetc.errors import DeliveryCode

        if not _main.config.MEME_ENABLED:
            return False
        if not _main.is_allowed_group(group_id, _main.config.ALLOWED_GROUPS):
            return False
        msg_id = str(getattr(message, "id", "") or "")
        if not proactive and not msg_id:
            # 缺失被动回复凭证不擅自转主动发送。
            return False
        engine = _main.memes.get_engine()
        trigger = engine.resolve(category, group_id=group_id, text=text)
        if trigger is None:
            # 素材目录缺失/为空，或未拼出公网地址：安静跳过，不报错也不阻塞。
            return False
        mode = _main.DeliveryMode.ACTIVE if proactive else _main.DeliveryMode.PASSIVE

        async def send_image(_message_format=None):
            try:
                image = _main.db.get_meme_file_cache(group_id, trigger.relative_path)
            except _main.sqlite3.Error:
                image = ""
            if not image:
                uploaded = await _main.asyncio.wait_for(
                    self.client.upload_group_media(group_id, trigger.url),
                    timeout=_main.notifications.SEND_TIMEOUT_SECONDS,
                )
                if not uploaded:
                    return _main.DeliveryResult(False, DeliveryCode.PLATFORM_ERROR,
                                          self.protocol, mode, detail="media_upload_failed")
                image, ttl_seconds = uploaded
                try:
                    _main.db.save_meme_file_cache(group_id, trigger.relative_path, image,
                                            ttl_seconds=ttl_seconds)
                except _main.sqlite3.Error:
                    logger.warning("表情包 file_info 缓存写入失败，本次仍继续发送")
            seq = 1
            if not proactive:
                seq = _replies._clamp_reply_seq(_replies._reply_sequence.get() + 1)
                _replies._reply_sequence.set(seq)
            return await _main.notifications.send_group_image(
                self.client, group_id, image,
                reply_to=None if proactive else msg_id, reply_seq=seq,
            )

        try:
            result = (await self._deliver_active_group(group_id, send_image)
                      if proactive else await send_image())
            if result:
                engine.record_sent(group_id, trigger.relative_path)
            return bool(result)
        except Exception:
            logger.exception("表情包发送失败，跳过")
            return False

    async def _persona_state_job(self) -> None:
        """每日生活状态切换：从内置语料随机取一句写入 bot_daily_state。"""
        try:
            status = await _main.persona_chat.daily_state_job()
            logger.info("机器人生活状态已切换：%s", status)
        except Exception:
            # 状态机失败只是聊天少一句当下状态，不能影响其它定时任务。
            logger.exception("生活状态切换失败，保留原状态")

    def _nickname_of(self, message: _main.QQBotMessage, member_id: str) -> str:
        """取官方事件中可用的发言人昵称。

        取不到就退化成「群友+尾号」：拟人聊天需要称呼，但绝不能因此丢消息。
        """
        raw = getattr(message, "raw", None)
        if isinstance(raw, dict):
            author = raw.get("author")
            if isinstance(author, dict):
                for key in ("username", "nickname"):
                    name = author.get(key)
                    if isinstance(name, str) and name.strip():
                        return name.strip()[:16]
        tail = str(member_id or "")[-4:]
        return f"群友{tail}" if tail else "群友"

    async def _handle_private_chat(self, message: _main.QQBotMessage, text: str,
                                   user_id: str) -> bool:
        """私聊接话使用独立用户滑窗，回执仅关联当前 C2C 入站消息。"""
        from cdutetc import private_chat

        if not private_chat.is_chat_message(text):
            return False
        allowed, gate_reply = self._qa_gate(user_id, text)
        if not allowed:
            if gate_reply:
                await self._send_private_result(user_id, gate_reply)
            return True
        if not _main.config.QA_SOCIAL_CHAT_ENABLED:
            return True
        reply = private_chat.local_persona_reply(text)
        if not reply and _main.content_filter.is_social_chat(text):
            reply = _main.llm._local_social_reply(text)
        if not reply:
            fallback = "我在呢，慢慢说～"
            reply = fallback
            if _main.config.PERSONA_CHAT_ENABLED and _main.config.LLM_API_KEY:
                memory = getattr(self, "_private_chat_memory", None)
                if memory is None:
                    memory = _main.persona_chat.PersonaMemory()
                    self._private_chat_memory = memory
                nickname = self._nickname_of(message, user_id)
                system, user = _main.persona_chat.build_conversation(
                    nickname=nickname, text=text, status_text=_main.db.DEFAULT_DAILY_STATUS,
                    facts=[], context=memory.recent(user_id), business_facts="",
                )
                try:
                    raw = await _main.asyncio.wait_for(
                        _main.persona_chat.chat(system, user), timeout=8.0,
                    )
                    filtered = _main.content_filter.sanitize_output(
                        raw if isinstance(raw, str) else "",
                        max_length=_main.config.QA_OUTPUT_MAX_LENGTH,
                        max_link_length=_main.config.QA_OUTPUT_MAX_LINK_LENGTH,
                    )
                    body, _category = _main.persona_chat.split_meme_tag(filtered)
                    reply = _main.persona_chat.clamp_reply(body) or fallback
                except Exception:
                    logger.exception("私聊接话失败，使用本地回应")
                memory.observe(user_id, nickname, text)
                memory.observe(user_id, _main.persona_chat.bot_names()[0], reply)
        reply = _main.content_filter.sanitize_output(
            reply, max_length=_main.config.QA_OUTPUT_MAX_LENGTH,
            max_link_length=_main.config.QA_OUTPUT_MAX_LINK_LENGTH,
        )
        await self._send_private_result(user_id, reply)
        return True

    async def _handle_persona_chat(self, message: _main.QQBotMessage, text: str,
                                   group_id: str, member_id: str,
                                   *, remember: bool = True,
                                   max_chars: int = 0,
                                   max_sentences: int = 0,
                                   lightweight: bool = False) -> bool:
        """@机器人 的日常闲聊/接梗：低 Token 拟人回复 + 记忆积累 + 表情包顺风车。

        lightweight（纯文本喊名接 LLM 的均衡省 Token 模式）：人设 system 完整保留，
        仅把群聊上下文压到 4 条、Prompt 预算压到 600、回复上限压到 100 Token。
        失败/无 Key/空输出一律返回 False，由调用方回退本地固定句。

        Returns:
            True  → 已回应（纯文本、或仅一张表情包），调用方直接返回；
            False → 没发出任何东西（未配模型 / 模型失败 / 回复被过滤），
                    调用方继续原有兜底逻辑，关掉开关或没配 Key 时功能不退化。
        """
        if not (_main.config.PERSONA_CHAT_ENABLED and _main.config.QA_SOCIAL_CHAT_ENABLED
                and _main.db.group_switch_enabled(group_id, "qa_enabled")):
            return False
        body = (text or "").strip()
        if not body or body.startswith("/"):
            return False
        # 入口已经扣过调用额度；这里重查身份和内容，不重复消耗限流额度。
        if not self._qa_gate(member_id, body, check_rate_limit=False)[0]:
            return False
        memory = _main.persona_chat.get_memory()
        nickname = self._nickname_of(message, member_id)
        # 上下文要在记录本条之前取，否则「要回的那句话」会在上下文里出现两次。
        context = memory.recent(group_id)
        if lightweight:
            # 均衡省 Token：上下文只留最近 4 条；人设 system 完整保留不裁剪。
            context = context[-4:]
            prompt_budget = 600
            reply_max_tokens = 100
        else:
            prompt_budget = _main.persona_chat.PROMPT_TOKEN_BUDGET
            reply_max_tokens = _main.persona_chat.REPLY_MAX_TOKENS
        if remember:
            memory.observe(group_id, nickname, body)
        # 0 Token 个人记忆：命中口语模板就用确定性规则截一句标签，绝不额外调模型。
        fact = _main.persona_chat.extract_memory_fact(body)
        if fact:
            try:
                _main.db.add_memory_fact(group_id, member_id, fact)
            except _main.sqlite3.Error:
                logger.warning("个人记忆写入失败，已跳过本条记忆", exc_info=True)
        facts: list[str] = []
        try:
            facts = _main.db.list_memory_facts(group_id, member_id,
                                         limit=_main.persona_chat.MEMORY_FACTS_INJECT)
        except _main.sqlite3.Error:
            logger.warning("个人记忆读取失败，本次不注入记忆", exc_info=True)
        try:
            status_text = _main.db.get_daily_state()["status_text"]
        except _main.sqlite3.Error:
            status_text = _main.db.DEFAULT_DAILY_STATUS
            logger.warning("生活状态读取失败，本次用默认状态", exc_info=True)
        from cdutetc import qa_context

        current = _main.datetime.now(_main.ZoneInfo("Asia/Shanghai"))
        try:
            business_facts = qa_context.compact_business_facts(group_id, body, now=current)
        except Exception:
            logger.warning("本群聊天事实读取失败，本次不注入业务事实", exc_info=True)
            business_facts = ""
        system, user = _main.persona_chat.build_conversation(
            nickname=nickname, text=body, status_text=status_text,
            facts=facts, context=context, business_facts=business_facts, now=current,
            budget=prompt_budget,
        )
        try:
            raw_reply = await _main.persona_chat.chat(
                system, user, **({"max_tokens": reply_max_tokens} if lightweight else {}))
        except Exception:
            # 闲聊失败一律回退原有回复链路：不能因为模型抖一下就整条消息无人应。
            logger.exception("拟人聊天调用失败，回退原有回复链路")
            return False
        # 先过滤完整输出，再拆标签/截短，避免截断掩盖敏感内容。
        filtered_reply = _main.content_filter.sanitize_output(
            raw_reply if isinstance(raw_reply, str) else "",
            max_length=_main.config.QA_OUTPUT_MAX_LENGTH,
            max_link_length=_main.config.QA_OUTPUT_MAX_LINK_LENGTH,
        )
        reply, category = _main.persona_chat.split_meme_tag(filtered_reply)
        from cdutetc import private_chat

        if reply and not private_chat.valid_persona_reply(reply):
            return False
        if reply and (max_chars or max_sentences):
            # 主动插话走这条路径：句数先压到 1~2 句，再压字数，绝不整段小作文。
            reply = _main.persona_chat.clamp_reply(
                reply, max_chars=max_chars, max_sentences=max_sentences)
        if not reply and not category:
            return False
        text_sent = False
        if reply:
            text_sent = await self._safe_reply(message, reply)
            # 机器人自己那句也进滑窗，后续几轮才不会答非所问。
            if text_sent:
                memory.observe(group_id, _main.persona_chat.bot_names()[0], reply)
        # 表情包顺风车：分类由本条回复明确指定，直接复用阶段二链路，
        # 不走关键词判定（因此天然跳过关键词冷却），任一前置条件缺失即安静不发。
        # 模型也可能只给一个 [MEME:xx]、不写字，这时只发图、不发空文本。
        image_sent = False
        if category and _main.config.MEME_ENABLED:
            image_sent = await self._send_meme(message, group_id, category, text=body)
        if text_sent or image_sent:
            self._note_bot_spoke(group_id)
        return bool(text_sent or image_sent)

    async def _maybe_persona_interject(self, message: _main.QQBotMessage, text: str,
                                       group_id: str) -> bool:
        """群聊高潮时的低频主动插话：条数、冷却、静默窗、概率四重闸门全过才说话。

        普通群消息先入滑窗（作为后续上下文），再交给闸门判定；闸门不通过就
        完全安静，绝不让机器人变成刷屏源头。机器人刚发过言（复读、表情包、
        上一句回复）时闸门直接拦住，与娱乐模块共用同一条说话节奏。
        """
        if not (_main.config.PERSONA_CHAT_ENABLED and _main.config.QA_SOCIAL_CHAT_ENABLED
                and _main.db.group_switch_enabled(group_id, "qa_enabled")):
            return False
        from cdutetc import private_chat

        if not private_chat.can_interject(text):
            return False
        member_id = self._extract_member_id(message)
        # 不合格的输入连上下文也不记录，避免借下一位群友的回复绕过过滤。
        if not self._qa_gate(member_id, text, check_rate_limit=False)[0]:
            return False
        try:
            if not _main.persona_chat.get_gate().observe(group_id, reserve_only=True):
                _main.persona_chat.get_memory().observe(
                    group_id, self._nickname_of(message, member_id), text)
                return False
        except Exception:
            logger.exception("插话闸门判定失败，保持安静")
            return False
        # 普通聊天不扣问答额度，只有实际准备调模型时才计一次。
        if not self._qa_gate(member_id, text)[0]:
            return False
        # 命中的这句由 handler 在截取历史之后记录，避免它在 prompt 出现两遍。
        # 插话不比正对话：只接 1~2 句、40 字内，多了就是刷屏。
        return await self._handle_persona_chat(
            message, text, group_id, member_id,
            max_chars=_main.persona_chat.INTERJECT_MAX_CHARS,
            max_sentences=_main.persona_chat.INTERJECT_MAX_SENTENCES,
        )
