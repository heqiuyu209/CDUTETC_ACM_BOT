"""本群归档与测试数据清理：明确选群、预览确认和独立权限校验。"""

from __future__ import annotations

import logging
import re

from cdutetc import admin_session, config, roles
from cdutetc.errors import AppErrorCode
from cdutetc.handlers._shared import _error_reply
from cdutetc.utils import is_super_admin

logger = logging.getLogger(__name__)

CLEANUP_COMMANDS = frozenset({
    "清理预览", "清理确认", "标记测试", "取消测试标记", "恢复比赛", "清理规则",
})

_MODES = {"过期比赛": "expired_contests", "测试数据": "test_data"}
_TYPE_LABELS = {
    "competition": "比赛", "legacy_contest": "旧比赛", "collection": "征集",
    "training_session": "课程", "custom_reminder": "自定义提醒",
    "question": "问题", "faq": "FAQ", "publication": "发布申请",
}
_OBJECT_TYPES = {label: key for key, label in _TYPE_LABELS.items()}
_OBJECT_TYPES.update({key: key for key in _TYPE_LABELS})
_OBJECT_TYPES["faq"] = "faq"
_OBJECT_TYPES["提醒"] = "custom_reminder"


def _reply(group_id: str, text: str) -> list[dict]:
    return [{"group_id": group_id, "text": text}]


def with_cleanup_header(result: list[dict], sender: str) -> list[dict]:
    """清理回执只显示明确选中的群，统一工作区标题不能回落到默认群。"""
    if not is_super_admin(sender):
        return result
    selected = admin_session.get_active_group(sender)
    head = f"📌【清理目标群：{selected}】" if selected else "🌐【清理目标群：未选群】"
    return [{**item, "text": f"{head}\n{item['text']}"} if item.get("text") else item for item in result]


def _days(value: str) -> int:
    if not re.fullmatch(r"[1-9]\d*天?", value):
        raise ValueError("保留天数须为正整数，例如 30天。")
    return int(value.removesuffix("天"))


def _object_id(value: str) -> int:
    if not re.fullmatch(r"[1-9]\d*", value):
        raise ValueError("编号须为正整数。")
    return int(value)


def _skipped_count(value) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        return sum(count for count in value.values() if isinstance(count, int))
    return len(value or ())


def handle_cleanup_command(cmd: str, args: str, group_id: str, sender: str,
                           *, private: bool = False) -> list[dict]:
    """私聊只信已选群会话，群内老师只能预览，所有写入仅超管。"""
    if private:
        if not is_super_admin(sender):
            return _reply(group_id, "私聊清理仅对超级管理员开放，请在本群预览。")
        target = admin_session.get_active_group(sender)
        if not target:
            return _reply(group_id, "请先 /选群 <序号|群名> 明确选择目标群，再执行清理命令。")
        group_id = target
    if group_id not in config.ALLOWED_GROUPS:
        return _reply(group_id, "目标群未授权，清理命令未执行。")
    if roles.resolve_admin_scope(sender, group_id) is None:
        return _reply(group_id, "清理预览仅对本群老师和超级管理员开放。")
    if cmd != "清理预览" and not is_super_admin(sender):
        return _reply(group_id, "清理写操作与规则管理仅对超级管理员开放；老师可用 /清理预览。")

    try:
        from cdutetc import group_cleanup

        tokens = (args or "").split()
        if cmd == "清理预览":
            if not tokens or tokens[0] not in _MODES or len(tokens) > 2:
                raise ValueError("用法：/清理预览 过期比赛 [30天]，或 /清理预览 测试数据。")
            mode = _MODES[tokens[0]]
            if mode == "test_data" and len(tokens) != 1:
                raise ValueError("测试数据预览不需要天数：/清理预览 测试数据。")
            days = (0 if mode == "test_data" else _days(tokens[1]) if len(tokens) == 2 else 30)
            preview = group_cleanup.preview_cleanup(group_id, mode, days=days, operator=sender)
            items = preview["items"]
            skipped = _skipped_count(preview.get("skipped"))
            if not items:
                return _reply(group_id, f"本群没有符合条件的{tokens[0]}，跳过 {skipped} 项。")
            lines = [f"【本群{tokens[0]}预览】共 {len(items)} 项，跳过 {skipped} 项。"]
            for item in items[:20]:
                label = _TYPE_LABELS.get(item["type"], item["type"])
                title = str(item.get("title", "")).replace("\n", " ")[:100]
                lines.append(f"{label} #{item['id']}：{title}")
            if len(items) > 20:
                lines.append(f"另有 {len(items) - 20} 项。")
            lines.append("共享比赛在本群归档，可用 /恢复比赛 <编号> 恢复；旧比赛记录按群清理。" if mode == "expired_contests"
                         else "已标记的测试数据会清理，执行前保存本机备份。")
            if is_super_admin(sender):
                lines.append(f"超管确认：/清理确认 {preview['token']}")
            else:
                lines.append("需要执行时，请超管在本群重新预览并确认。")
            return _reply(group_id, "\n".join(lines))

        if cmd == "清理确认":
            if len(tokens) != 1:
                raise ValueError("用法：/清理确认 <预览确认码>。")
            result = group_cleanup.apply_cleanup(group_id, tokens[0], operator=sender)
            summary = "、".join(
                f"{_TYPE_LABELS.get(kind, kind)} {count} 项" for kind, count in result["cleared"].items()
            ) or "0 项"
            text = f"本群清理完成：{summary}；跳过 {_skipped_count(result.get('skipped'))} 项。"
            if result.get("backup"):
                text += "\n执行前备份已保存到本机。"
            return _reply(group_id, text)

        if cmd in {"标记测试", "取消测试标记"}:
            if len(tokens) != 2 or tokens[0] not in _OBJECT_TYPES:
                raise ValueError(f"用法：/{cmd} 比赛|旧比赛|课程|提醒|征集|FAQ|问题|发布申请 <编号>。")
            kind, obj_id = _OBJECT_TYPES[tokens[0]], _object_id(tokens[1])
            operation = group_cleanup.mark_test if cmd == "标记测试" else group_cleanup.unmark_test
            operation(group_id, kind, obj_id, operator=sender)
            verb = "已标记测试数据" if cmd == "标记测试" else "已取消测试标记"
            return _reply(group_id, f"本群{_TYPE_LABELS[kind]} #{obj_id}：{verb}。")

        if cmd == "恢复比赛":
            if len(tokens) != 1:
                raise ValueError("用法：/恢复比赛 <比赛编号>。")
            restored = group_cleanup.restore_competition(group_id, _object_id(tokens[0]), operator=sender)
            return _reply(group_id, "比赛已恢复到本群。" if restored else "本群未找到可恢复的比赛归档。")

        if cmd == "清理规则":
            if tokens:
                if len(tokens) != 3 or tokens[0] not in _MODES or tokens[2] not in {"开启", "关闭"}:
                    raise ValueError("用法：/清理规则，或 /清理规则 过期比赛|测试数据 <天数> 开启|关闭。")
                group_cleanup.set_rule(group_id, _MODES[tokens[0]], _days(tokens[1]),
                                       tokens[2] == "开启", operator=sender)
            rules = group_cleanup.get_rules(group_id)
            lines = ["【本群清理规则】"]
            for label, mode in _MODES.items():
                rule = rules[mode]
                lines.append(f"{label}：{'开启' if rule['enabled'] else '关闭'}，保留 {rule['days']} 天。")
            lines.append("每天北京时间 04:10 静默执行，不向群发送消息。")
            return _reply(group_id, "\n".join(lines))
        raise ValueError("未知清理命令。")
    except ValueError as exc:
        return _error_reply(group_id, AppErrorCode.VALIDATION, str(exc))
    except Exception:
        logger.exception("清理命令处理失败 [%s]", cmd)
        return _error_reply(group_id, AppErrorCode.INTERNAL)
