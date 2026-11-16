"""回复发送链 Mixin：私聊/群聊结果回执、被动回复序列号、合并回复、未授权提示、运行期状态。"""

from __future__ import annotations

import logging

from cdutetc import main as _main
from cdutetc import publish_format as _fmt

logger = logging.getLogger(__name__)


_reply_sequence = _main.contextvars.ContextVar("qq_reply_sequence", default=0)


_private_reply_context = _main.contextvars.ContextVar("qq_private_reply_context", default=None)


_ACTIVE_SEND_TIMEOUT_SECONDS = 20


# 官方协议严格限制被动回复 msg_seq 在 [1,5]，第 6 条会被平台判为 INVALID_REQUEST。
_MAX_PASSIVE_REPLY_SEQ = 5


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


# 未授权群提示的冷却时间记录：{group_id: 上次提示的 monotonic 时间}
_unauthorized_notice_at: dict[str, float] = {}


class ReplyMixin:
    """按职责拆分的 ACMBot 能力 Mixin。"""

    async def _notify_publish_group(self, pending_item: dict, cid: int, operator_id: str = "") -> None:
        """超管同意后发送新比赛通知：只发给该比赛的通知范围（默认仅来源群）。"""
        info = _main.json.loads(pending_item.get("fields_json") or "{}")
        notice = _fmt._build_publish_notice(info, cid)
        detail = await self._publish_to_groups(cid, notice)
        await self._send_private_result(operator_id, f"比赛 #{cid} 已获批。\n{detail}")

    async def _publish_to_groups(self, cid: int, notice: str) -> str:
        try:
            targets = sorted(_main.db.get_competition_target_groups(cid))
        except (_main.sqlite3.Error, ValueError):
            logger.exception("比赛 #%s 通知范围读取失败", cid)
            return "群通知未发送：[internal] 通知范围读取失败，比赛资料已保存。"

        results = await self._send_group_notices([{"group_id": gid, "text": notice} for gid in targets])
        for gid, result in results:
            if not result.ok:
                logger.warning("比赛 #%s 群 %s 通知未发送 [%s]", cid, gid, result.code.value)
        return _fmt._notification_details(results)

    async def _send_group_notices(self, notices: list[dict]) -> list[tuple[str, _main.DeliveryResult]]:
        async def send(item):
            try:
                return await self._send_group_result(item["group_id"], item["text"])
            except Exception as error:
                logger.exception("群 %s 主动通知处理失败", item["group_id"])
                return _main.result_from_exception(error, protocol=self.protocol, mode=_main.DeliveryMode.ACTIVE)

        results = await _main.notifications.map_by_group(notices, send)
        return [(item["group_id"], result) for item, result in zip(notices, results)]

    async def _send_private_result(self, user_id: str, text: str) -> _main.DeliveryResult:
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
            return await _main.asyncio.wait_for(
                _main.notifications.send_private(self.client, user_id, text, **kwargs),
                timeout=_ACTIVE_SEND_TIMEOUT_SECONDS,
            )
        except Exception as error:
            logger.warning("私聊发送异常 [%s]", type(error).__name__)
            mode = _main.DeliveryMode.PASSIVE if self.supports_passive_reply and kwargs else _main.DeliveryMode.ACTIVE
            return _main.result_from_exception(error, protocol=self.protocol, mode=mode)

    async def _safe_reply(self, message: _main.QQBotMessage, content: str) -> bool:
        """只回复当前入站群消息，官方适配不得复用历史ID或转主动重发。"""
        # 超管私聊录入比赛时，入参是 SimpleNamespace 轻量消息：没有真实群上下文，
        # 一律走私聊回执，避免落进 send_group 的群白名单校验。
        private_uid = getattr(message, "_private_uid", None)
        if private_uid:
            result = await self._send_private_result(str(private_uid), content)
            return bool(result)
        group_id = message.group_id
        if not _main.is_allowed_group(group_id, _main.config.ALLOWED_GROUPS):
            return False
        seq = _clamp_reply_seq(_reply_sequence.get() + 1)
        _reply_sequence.set(seq)
        result = await _main.notifications.send_group(
            self.client, group_id, content,
            reply_to=str(getattr(message, "id", "") or ""), reply_seq=seq,
        )
        return bool(result)

    async def _notice_unauthorized_group(self, message: _main.QQBotMessage,
                                         group_id: str) -> None:
        """未授权群被明确喊话时，回一句带群号的提示，方便管理员取号后授权。

        只在「@ 了机器人」或「发 /命令」时回复，避免被拉进群后对每条闲聊都插话；
        同一群有冷却时间，防止刷屏。提示本身不提供任何业务功能。
        """
        if not _main.config.NOTICE_UNAUTHORIZED_GROUP:
            return
        raw = message.content or ""
        addressed = _main.is_addressed_to_bot(message)
        text = _main.strip_at(raw)
        if not (addressed or text.startswith("/")):
            return
        now = _main.time.monotonic()
        if now - _unauthorized_notice_at.get(group_id, 0.0) < _main.config.NOTICE_UNAUTHORIZED_INTERVAL:
            return
        _unauthorized_notice_at[group_id] = now
        try:
            seq = _clamp_reply_seq(_reply_sequence.get() + 1)
            _reply_sequence.set(seq)
            await _main.notifications.send_group(
                self.client, group_id,
                "本群尚未授权。\n"
                f"请超管私聊机器人发送：/加群 {group_id}",
                reply_to=str(getattr(message, "id", "") or ""), reply_seq=seq,
            )
        except Exception:
            logger.warning("未授权群提示发送失败: %s", group_id)

    async def _send_group_result(self, group_id: str, text: str, *, permission_probe: bool = False,
                                 message_format: str | None = None):
        """主动文本通知入口，返回可审计的结构化结果。"""
        async def send(selected_format):
            kwargs = {"message_format": selected_format} if selected_format == "markdown" else {}
            return await _main.notifications.send_group(self.client, group_id, text, **kwargs)

        return await self._deliver_active_group(
            group_id, send, permission_probe=permission_probe, message_format=message_format)

    async def _deliver_active_group(self, group_id: str, send, *,
                                    permission_probe: bool = False,
                                    message_format: str | None = None):
        """主动文本/图片共用开关、原子恢复领取、超时与投递结果处理。"""
        from cdutetc.errors import DeliveryCode
        if not _main.is_allowed_group(group_id, _main.config.ALLOWED_GROUPS):
            return _main.DeliveryResult(False, DeliveryCode.TARGET_NOT_ALLOWED, self.protocol)
        if not _main.config.GROUP_ACTIVE_MESSAGES_ENABLED:
            return _main.DeliveryResult(False, DeliveryCode.DISABLED, self.protocol)
        if message_format is not None and message_format not in ("text", "markdown"):
            return _main.DeliveryResult(False, DeliveryCode.INVALID_REQUEST, self.protocol,
                                  detail="invalid_message_format")
        recovering = False
        if self.supports_passive_reply and not permission_probe:
            block = _main.db.get_group_notification_block(self.protocol, group_id)
            if block:
                if not _main.db.claim_group_notification_retry(self.protocol, group_id):
                    return _main.DeliveryResult(False, DeliveryCode.ACTIVE_FORBIDDEN, self.protocol,
                                          detail="active_permission_paused", platform_code=block["platform_code"])
                recovering = True
        if self.supports_passive_reply:
            message_format = message_format or _main.db.get_group_notification_format(self.protocol, group_id)
        else:
            message_format = "text"
        try:
            result = await _main.asyncio.wait_for(
                send(message_format),
                timeout=_ACTIVE_SEND_TIMEOUT_SECONDS,
            )
        except Exception as error:
            result = _main.result_from_exception(error, protocol=self.protocol, mode=_main.DeliveryMode.ACTIVE)
        if self.supports_passive_reply:
            if result.code == DeliveryCode.ACTIVE_FORBIDDEN:
                _main.db.block_group_notifications(self.protocol, group_id, result.code.value, result.platform_code)
                logger.warning("QQ主动发送被平台拒绝，已暂停该群；五分钟后有待发消息时重试，或 /通知测试 群 立即验证 [40034105]")
            elif result.code == DeliveryCode.QUOTA_EXHAUSTED:
                # 配额耗尽当天不进行五分钟恢复尝试；保持次日自动恢复语义。
                _main.db.block_group_notifications(self.protocol, group_id, result.code.value, result.platform_code)
                logger.warning(
                    "[主动发信限流] 群 %s 今日主动消息配额已耗尽(11253/11263)，停止重试并触发今日熔断",
                    group_id,
                )
            elif result.ok and (permission_probe or recovering):
                try:
                    _main.db.confirm_group_notification_channel(self.protocol, group_id, message_format)
                except _main.sqlite3.Error:
                    logger.exception("平台已接受主动消息，但本群通知格式和暂停状态保存失败")
                    result = _main.replace(result, detail="channel_state_save_failed")
        return result
