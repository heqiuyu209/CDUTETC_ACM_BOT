"""发布交互 Mixin：发布主流程、审核决策回调、通知通道自检。"""

from __future__ import annotations

import logging

from cdutetc import main as _main
from cdutetc import publish_format as _fmt
from cdutetc.bot import replies as _replies

logger = logging.getLogger(__name__)


class PublishMixin:
    """按职责拆分的 ACMBot 能力 Mixin。"""

    async def _handle_publish_decision(self, uid: str, content: str) -> bool:
        """处理超管私聊的发布审批回复。返回 True 表示已处理并已回复过。

        可接受：`同意` / `同意 3` / `通过 3` / `拒绝 3` / `拒绝 3 原因`。
        不带编号时，若只有一条待确认申请则直接作用于它，否则列出清单。
        """
        match = _fmt._PUBLISH_DECISION_RE.match((content or "").strip())
        if not match:
            return False
        approve = match.group("action") in ("同意", "通过", "批准", "approve", "ok", "y")
        pid_text = match.group("pid")
        note = (match.group("note") or "").strip()
        pending = _main.db.list_pending_publications()
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
                cid, created = _main.db.approve_pending_publication(pid, operator=uid)
                pending_item = _main.db.get_pending_publication(pid) or {}
                submitter_text = _fmt._format_submitter(pending_item.get("submitted_by") or "")
                if created:
                    logger.info(
                        "发布申请 #%s 已入库为比赛 #%s｜提交人=%s｜审批人=%s",
                        pid, cid, submitter_text, uid,
                    )
                    await self._send_private_result(
                        uid, f"已入库：#{cid} {pending_item.get('name') or ''}。"
                        f"提交人：{submitter_text}。"
                        f"仅为有比赛时间的目标群安排开赛前 {_fmt._format_remind_offsets()}的未来节点；"
                        "时间未定或已过节点不建计划。正在通知目标群。"
                    )
                    await self._notify_publish_group(pending_item, cid, uid)
                else:
                    await self._send_private_result(
                        uid, f"该比赛已存在（编号 {cid}），未重复入库；"
                        "通知范围和未来提醒计划已按本次申请同步。"
                    )
            else:
                if not _main.db.reject_pending_publication(pid, operator=uid, note=note):
                    await self._send_private_result(uid, f"申请 #{pid} 已处理过，无需重复操作。")
                    return True
                await self._send_private_result(uid, f"已拒绝申请 #{pid}，未入库。")
        except ValueError:
            await self._send_private_result(uid, _main.safe_error_hint(_main.AppErrorCode.VALIDATION))
        return True

    async def _handle_publish(self, message: _main.QQBotMessage, text: str,
                              group_id: str, member_id: str):
        """发布命令：老师/超管权限校验 → 抓取 → 提取 → 校验 → 私聊超管确认。"""
        # 超管继承老师全部权限（含发布），无需再额外加入老师白名单
        if not _main.can_manage_group(member_id):
            await self._safe_reply(message, "抱歉，你没有发布比赛信息的权限~")
            return
        if _main.config.PUBLISH_CONFIRM_ENABLED and not _main.config.SUPER_ADMIN:
            await self._safe_reply(message, "还没配置发布审核人，暂时无法提交比赛，请联系管理员补齐。 [validation]")
            return

        # 有链接就抓官网正文；没有链接也允许发布——直接把老师发来的文字
        # 当作信息源，「有什么先存什么」，不强迫补链接。
        url = _main.extract_url(text) or ""
        if url:
            try:
                page = await _main.fetch_text(url)
            except _main.FetchError as e:
                # 抓取失败不中断：退化为按老师原文提取，保证信息不丢
                logger.warning("链接抓取失败，改为按原文提取: %s", e)
                page = ""
                await self._safe_reply(message, f"链接打不开（{e}），我先按你发来的文字保存。")
        else:
            page = ""

        # 老师原文永远排在最前：官网首页往往只有通用介绍，抢权会吞掉真实比赛信息
        if page:
            raw_text = (
                f"【老师直接通知（最高优先级，必须优先提取其中的时间、赛道与报名要求）】\n{text}\n\n"
                f"【官网链接抓取参考（仅用于补充老师通知中未提及的届次、主办方等背景信息，"
                f"严禁覆盖老师通知中的时间和赛道）】\n{page}"
            )
        else:
            raw_text = f"老师原文：{text}"
        try:
            info = None if url else _main.parse_simple_publication(text, now=_main.datetime.now(_main.ZoneInfo(_main.config.TZ_NAME)))
        except ValueError as error:
            await self._safe_reply(message, f"比赛时间未保存：{error}，请核对后再发。 [validation]")
            return
        err = None
        if info is None:
            info, err = await _main.extract_contest(raw_text)
        if err is not None or not info:
            await self._safe_reply(
                message,
                f"信息提取失败：{err or '未知原因'}。"
                "可以再发一次，尽量包含比赛名称和时间；实在没有也能先存个名字~",
            )
            return

        # 有官网正文时再做一次「老师原文」确定性兜底：网页泛化内容不得吞掉原文写明的日期/赛道
        if page:
            info = self._apply_teacher_text_fallback(info, text)

        info["source_link"] = url
        try:
            competition_info = _main.map_competition_info(info)
        except ValueError as e:
            await self._safe_reply(message, f"信息校验失败：{e}")
            return

        # 时间解析不了（如「下学期开学初」）就降级为待定，原文记入简介，不算失败
        competition_info, _ = _fmt._sanitize_publish_times(competition_info)

        # 查重：名称+届次+比赛时间完全一致才判为重复（不看链接，转发消息也能查出）
        duplicate = _main.db.find_duplicate_competition(competition_info)
        if duplicate is not None and duplicate.get("status") == "confirmed":
            cid = duplicate["id"]
            # 同一场比赛在另一个群再次发布：不新建记录，只把本群加入通知范围，
            # 这样「本群发布的比赛在本群提醒」和「一场公开比赛通知多个群」都满足。
            scope = _main.db.get_competition_notify_groups(cid)
            if scope and group_id not in scope:
                scope.add(group_id)
                _main.db.set_competition_notify_groups(cid, scope, operator=f"publish:{member_id}")
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
        if _main.config.PUBLISH_CONFIRM_ENABLED:
            try:
                pid = _main.db.create_pending_publication(
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
            pending = _main.db.get_pending_publication(pid) or {}
            review = _fmt._build_publish_review(pending)
            sent = 0
            failures = []
            for admin in sorted(_main.config.SUPER_ADMIN):
                result = await self._send_private_result(admin, review)
                if result.ok:
                    sent += 1
                else:
                    failures.append(result.code.value)
                    logger.warning("发布申请 #%s 确认私聊推送失败 [%s]", pid, result.code.value)
            receipt = _fmt._build_publish_receipt(competition_info, status="待超管确认", ref=pid)
            if not failures:
                receipt += f"\n确认单已向{sent}位超管提交发送，请在私聊回复“同意 {pid}”或“拒绝 {pid}”。"
            else:
                codes = "、".join(f"[{code}]" for code in sorted(set(failures)))
                receipt += (f"\n超管私聊推送失败（成功{sent}/{len(_main.config.SUPER_ADMIN)}）：{codes}。"
                            f"申请 #{pid} 已保留，超管可私聊发送 /待确认列表 后回复“同意 {pid}”。")
            await self._safe_reply(message, receipt)
            logger.info(
                "发布申请 #%s 已提交超管确认：%s｜提交人=%s",
                pid, info.get("name"), _fmt._format_submitter(member_id),
            )
            return

        # 显式关闭确认流程时直接发布。
        try:
            cid, created = _main.create_published_competition_once(
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
        notice = _fmt._build_publish_notice(competition_info, cid)
        # 只发该比赛的通知范围（默认即发布它的群），不再全群广播
        detail = await self._publish_to_groups(cid, notice)
        await self._safe_reply(
            message,
            _fmt._build_publish_receipt(competition_info, status="已入库", ref=cid)
            + "\n" + detail,
        )

    async def _notification_test(self, uid: str, args: str, *, message=None) -> str:
        """主动发一条测试消息；不复用当前msg_id来冒充主动通道可用。"""
        if not _main.is_super_admin(uid):
            return _main.safe_error_hint(_main.AppErrorCode.PERMISSION)
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
            try:
                result = await _main.asyncio.wait_for(
                    _main.notifications.send_private(self.client, target, text),
                    timeout=_replies._ACTIVE_SEND_TIMEOUT_SECONDS,
                )
            except Exception as error:
                result = _main.result_from_exception(error, protocol=self.protocol, mode=_main.DeliveryMode.ACTIVE)
        code = getattr(result.code, "value", str(result.code))
        answer = f"通知测试：{'平台已接受' if result.ok else '未确认发送'} [{code}] {result.user_hint}"
        if kind == "群" and result.ok:
            if result.detail == "channel_state_save_failed":
                answer += " [internal] 平台已接受测试，但本地格式和暂停状态保存失败，尚未确认恢复，请检查数据库。"
            else:
                answer += " 主动通道本次测试通过，权限暂停已解除；仍有效的未来提醒会按计划发送。"
        if kind == "群" and self.supports_passive_reply and code not in ("disabled", "target_not_allowed"):
            used_format = requested_format or _main.db.get_group_notification_format(self.protocol, target)
            answer += "\n本次格式：" + ("Markdown" if used_format == "markdown" else "文本")
            if result.ok and result.detail != "channel_state_save_failed":
                answer += "，已记住本群格式，后续主动通知沿用。"
            query = getattr(self.client, "get_group_active_permission", None)
            try:
                state = await _main.asyncio.wait_for(query(target), timeout=6.0) if callable(query) else None
            except Exception:
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
