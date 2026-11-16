"""机器人入口与管线装配：日志配置、ACMBot 组合、客户端构建与启动。"""

# 本模块对外部符号的导入属 re-export：拆分后的子模块通过 `_main.<name>` 取用，
# 测试也通过 monkeypatch 改写这些符号，故不做未使用清理。
# ruff: noqa: F401

import asyncio
import contextvars
import json
import logging
import re
import sqlite3
import sys
import time
import types
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from cdutetc import (
    admin_session,
    chat_fun,
    commands,
    config,
    content_filter,
    crawler,
    daily_digest,
    llm,
    memes,
    notifications,
    persona_chat,
    roles,
    teacher_intent,
)
from cdutetc import database as db
from cdutetc import publish_format as _fmt
from cdutetc.alias_helper import format_group_name
from cdutetc.bot import events as _events
from cdutetc.bot import jobs as _jobs
from cdutetc.bot import persona as _persona_mix
from cdutetc.bot import publish as _publish_mix
from cdutetc.bot import replies as _replies
from cdutetc.bot.events import EventsMixin
from cdutetc.bot.jobs import JobsMixin
from cdutetc.bot.persona import PersonaMixin
from cdutetc.bot.publish import PublishMixin
from cdutetc.bot.replies import ReplyMixin
from cdutetc.clients.base import BaseBotClient, capabilities_of
from cdutetc.clients.qqbot import QQBotClient, QQBotMessage
from cdutetc.contest_fields import map_competition_info
from cdutetc.database import create_published_competition_once, init_db
from cdutetc.errors import (
    AppErrorCode,
    DeliveryMode,
    DeliveryResult,
    result_from_exception,
    safe_error_hint,
)
from cdutetc.fetcher import FetchError, fetch_text
from cdutetc.llm import extract_contest, normalize_time
from cdutetc.publish_text import (
    extract_declared_contest_time,
    extract_declared_tracks,
    is_timed_contest_announcement,
    parse_simple_publication,
)
from cdutetc.reminder import (
    check_contest_start_reminders,
    check_custom_reminders,
    check_general_reminders,
)
from cdutetc.utils import (
    admin_group_id,
    can_manage_group,
    extract_url,
    is_addressed_to_bot,
    is_allowed_group,
    is_super_admin,
    match_publish_command,
    message_mentions,
    parse_command,
    strip_at,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

logger = logging.getLogger(__name__)

class ACMBot(ReplyMixin, PublishMixin, PersonaMixin, JobsMixin, EventsMixin):
    """ACM Bot：按域由多个 Mixin 组合而成，行为与拆分前一致。"""

    def __init__(self, client: BaseBotClient | None = None):
        self.client = client
        self.sched: AsyncIOScheduler | None = None
        # 五层防护-4：频率防刷限流器（单用户滑动窗口，参数来自 config）
        self._qa_rate_limiter = content_filter.RateLimiter(
            config.QA_RATE_LIMIT_MAX_CALLS,
            config.QA_RATE_LIMIT_SECONDS,
        )
        # 后台收图任务：只持有引用防被 GC 回收，不等待、不影响消息响应。
        self._meme_tasks: set[asyncio.Task] = set()
        if client is not None:
            client.group_msg_handler = self.on_group_at_message_create
            client.private_msg_handler = self.on_private_message_create
            client.on_ready = self._on_ready

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

def _build_client():
    """创建唯一的 QQ 官方客户端。"""
    return QQBotClient(appid=config.QQBOT_APPID, secret=config.QQBOT_SECRET)


def run_bot() -> None:
    """启动入口：配置检查 → 创建官方客户端 → 常驻连接。"""
    if config.ENV_CREATED:
        need = "ACM_QQBOT_APPID、ACM_QQBOT_SECRET"
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

_CHILDREN = (_fmt, _replies, _publish_mix, _persona_mix, _jobs, _events)
_LOCAL = {
    "__name__", "__doc__", "__spec__", "__loader__", "__package__", "__path__",
    "__builtins__", "_CHILDREN", "_LOCAL", "_MainFacade", "ACMBot",
}


class _MainFacade(types.ModuleType):
    """入口门面：未在本模块定义的符号转发到拆分后的实现模块。"""

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


sys.modules[__name__].__class__ = _MainFacade
