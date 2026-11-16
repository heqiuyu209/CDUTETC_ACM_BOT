"""命令模块（Facade + 路由层）：对外保持 cdutetc.commands 的全部原有符号与语义。

业务实现已按域拆分到 cdutetc.handlers（contest_cmd / training_cmd / faq_cmd /
collection_cmd / admin_cmd / _shared）。本模块保留命令解析与分派路由，并通过模块级
__getattr__/__setattr__ 把其余符号的读写转发到真实实现子模块，
保证既有 import 与 monkeypatch 语义不变。
"""

import logging
import sys
import types
import asyncio
from cdutetc import config
from cdutetc.errors import AppErrorCode
from cdutetc.utils import is_super_admin, parse_command

from cdutetc.handlers import _shared as _shared
from cdutetc.handlers import admin_cmd as _admin_cmd
from cdutetc.handlers import collection_cmd as _collection
from cdutetc.handlers import contest_cmd as _contest
from cdutetc.handlers import faq_cmd as _faq_cmd
from cdutetc.handlers import training_cmd as _training


logger = logging.getLogger(__name__)


async def handle_command(text: str, group_id: str, sender: str,
                         client=None, *, private: bool = False,
                         active_group: str | None = None) -> list[dict] | None:
    """处理一条命令消息；超管私聊回执统一带工作区状态标牌（实现见 _handle_command_impl）。"""
    return _admin_cmd._with_private_header(
        await _handle_command_impl(text, group_id, sender, client,
                                   private=private, active_group=active_group),
        sender, private)


async def _handle_command_impl(text: str, group_id: str, sender: str,
                               client=None, *, private: bool = False,
                               active_group: str | None = None) -> list[dict] | None:
    """处理一条命令消息。

    - 非命令消息返回 None（由上层走 FAQ 匹配 / 模型问答 / 未答问题收集）。
    - 命令消息返回消息列表，每条含 group_id 与 text（text 为空表示无需发送）。
    - 命令名大小写不敏感：精确匹配失败时按 lower 兜底（兼容 /FAQ、/faq 等）。
    - 命令处理函数若为协程（如异步抓取）则在本循环内 await，避免嵌套 asyncio.run。
    - `private=True` 表示来自私聊：管理命令按全局作用域校验（仅超管），
      群聊按本群作用域校验；作用域为 None 时统一拦截，不进入任何管理逻辑。
    - `active_group` 仅对超管私聊生效：超管先 /选群 绑定了目标群后，
      私聊指令改按该群的单群视角执行（写库、查询、诊断都落到该群），
      回执仍由调用方走私聊，绝不发往真实群。
    """
    if not text.startswith("/"):
        return None
    was_private = private
    cmd, args = parse_command(text)
    if cmd is None:
        return [{"group_id": group_id, "text": "请写上命令名，例如 /帮助。"}]
    cmd = _admin_cmd.canonical_command(cmd)
    # 超管私聊 `/群配置 集训队`：先按名字/序号锁定工作区，再把参数清空，
    # 这样后续统一按「本群视角」展示，用户不必记 group_openid。
    if was_private and is_super_admin(sender) and cmd == "群配置":
        tokens = (args or "").split()
        if len(tokens) == 1 and tokens[0] != "全部":
            bound = _admin_cmd._bind_active_group(sender, tokens[0])
            if bound:
                active_group, args = bound, ""
    # 私聊绑定目标群（或默认工作区）时降维成「单群视角」：作用域按该群校验，展示范围也只到该群
    if (private and is_super_admin(sender) and cmd not in _admin_cmd.PRIVATE_SESSION_COMMANDS
            and cmd not in _admin_cmd.GROUP_ADMIN_COMMANDS and cmd != "修正"):
        target = active_group
        if not target and cmd in _admin_cmd.WORKSPACE_COMMANDS:
            # 省略群标识：直接落到超管工作区（重启后按库恢复，未选定回落正式群）
            from cdutetc import group_admin as _group_admin
            target = _group_admin.active_workspace(sender)
        if target and target in config.ALLOWED_GROUPS:
            group_id = target
            private = False
    if cmd in ("help", "帮助"):
        return [{"group_id": group_id,
                 "text": _admin_cmd.build_help(sender, args, private=was_private)}]
    scoped_cmd = cmd if cmd in _shared._STATUS_COMMANDS else next(
        (key for key in _shared._STATUS_COMMANDS if key.lower() == cmd.lower()), cmd)
    if scoped_cmd in _shared._STATUS_COMMANDS and _admin_cmd.admin_scope(sender, group_id, private=private) is None:
        return [{"group_id": group_id,
                 "text": _shared.SCOPE_DENIED_PRIVATE if private else _shared.SCOPE_DENIED_GROUP}]
    from cdutetc import group_admin
    handlers = {
        "群配置": lambda gid, uid, value: group_admin.handle_command(
            "群配置", value, uid, gid, private=private),
        "群提醒": lambda gid, uid, value: group_admin.handle_command(
            "群提醒", value, uid, gid, private=private),
        "周赛设置": lambda gid, uid, value: group_admin.handle_command(
            "周赛设置", value, uid, gid, private=private),
        "群老师": lambda gid, uid, value: group_admin.handle_command(
            "群老师", value, uid, gid, private=private),
        "修正": _contest.cmd_amend,
        "管理": lambda gid, uid, value: _admin_cmd.cmd_admin_panel(gid, uid, value),
        "待审列表": _contest.cmd_pending_list,
        "待审批准": _contest.cmd_pending_apply,
        "待审拒绝": _contest.cmd_pending_reject,
        "征集": _collection.cmd_collection_create,
        "报名": _collection.cmd_collection_register,
        "征集汇总": _collection.cmd_collection_summary,
        "征集关闭": _collection.cmd_collection_close,
        "校安排": _training.cmd_org_events,
        "通知列表": _contest.cmd_notice_list,
        "通知": _contest.cmd_notice_list,
        "课程列表": _training.cmd_training_list,
        "课程添加": _training.cmd_training_add,
        "课程取消": _training.cmd_training_cancel,
        "课程修改": _training.cmd_training_modify,
        "群开关": _admin_cmd.cmd_group_switch,
        "偷图": lambda gid, uid, value: _admin_cmd.cmd_meme_collect(gid, uid, value, private=private),
        "删图": lambda gid, uid, value: _admin_cmd.cmd_meme_delete(gid, uid, value, private=private),
        "提醒添加": _training.cmd_reminder_add,
        "提醒列表": _training.cmd_reminder_list,
        "提醒修改": _training.cmd_reminder_modify,
        "提醒删除": _training.cmd_reminder_delete,
        "FAQ列表": _faq_cmd.cmd_faq_list,
        "FAQ添加": _faq_cmd.cmd_faq_add,
        "FAQ删除": _faq_cmd.cmd_faq_delete,
        "待确认列表": _contest.cmd_pub_pending_list,
        "加群": lambda gid, uid, value: _admin_cmd.cmd_group_add(gid, uid, value, client=client),
        "删群": _admin_cmd.cmd_group_remove,
        "群列表": _admin_cmd.cmd_group_list,
        "命名群": _admin_cmd.cmd_label_group,
        "命名人": _admin_cmd.cmd_label_user,
        "命名列表": _admin_cmd.cmd_label_list,
        "认领此群": _contest.cmd_claim_group,
        "认领老师": _contest.cmd_claim_teacher,
        "加老师": _admin_cmd.cmd_teacher_add,
        "删老师": _admin_cmd.cmd_teacher_remove,
        "老师列表": _admin_cmd.cmd_teacher_list,
        "通知群": _admin_cmd.cmd_notify_groups,
        "提问": _faq_cmd.cmd_ask,
        "问题列表": _faq_cmd.cmd_question_list,
        "回答": _faq_cmd.cmd_answer_question,
        "抓取状态": _contest.cmd_fetch_status,
        "提醒状态": _training.cmd_reminder_status,
        "比赛状态": lambda gid, uid, value: _contest.cmd_contest_status(gid, uid, value, private=private),
        "群状态": lambda gid, uid, value: _contest.cmd_contest_status(gid, uid, value, private=private),
        "晨报预览": lambda gid, uid, value: _training.cmd_digest_preview(gid, uid, value, private=private),
        "抓取一次": _contest.cmd_trigger_fetch,
    }
    handler = handlers.get(cmd)
    if handler is None:
        lower = cmd.lower()
        for key, h in handlers.items():
            if key.lower() == lower:
                handler = h
                break
    if handler is None:
        return [{"group_id": group_id, "text": "没有这个命令，发送 /帮助 查看用法。"}]
    try:
        result = handler(group_id, sender, args)
        if asyncio.iscoroutine(result):
            result = await result
        return result
    except ValueError:
        logger.warning("命令参数校验失败 [%s]", AppErrorCode.VALIDATION.value)
        return _shared._error_reply(group_id, AppErrorCode.VALIDATION)
    except Exception:
        logger.exception("命令处理异常 [%s]", AppErrorCode.INTERNAL.value)
        return _shared._error_reply(group_id, AppErrorCode.INTERNAL)


_CHILDREN = (_contest, _training, _faq_cmd, _collection, _admin_cmd, _shared)
_LOCAL = {
    "__name__", "__doc__", "__spec__", "__loader__", "__package__", "__path__",
    "__builtins__", "_CHILDREN", "_LOCAL", "_CommandsFacade",
}


class _CommandsFacade(types.ModuleType):
    """命令门面：未在本模块定义的符号转发到 handlers 子模块。"""

    def __getattr__(self, name):
        for mod in _CHILDREN:
            if name in mod.__dict__:
                return mod.__dict__[name]
        raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")

    def __setattr__(self, name, value):
        if name in _LOCAL or name in self.__dict__:
            self.__dict__[name] = value
            return
        for mod in _CHILDREN:
            if name in mod.__dict__:
                setattr(mod, name, value)
                return
        self.__dict__[name] = value

    def __dir__(self):
        names = set(self.__dict__)
        for mod in _CHILDREN:
            names |= set(mod.__dict__)
        return sorted(n for n in names if not n.startswith("__"))


sys.modules[__name__].__class__ = _CommandsFacade
