"""超管私聊 /发布 录入比赛：作用域绑定当前工作区群、回执只走私聊。

覆盖验收点：
1) 未绑定工作区群时提示先 /群配置，不触发发布链路；
2) 已绑定但正文为空时返回用法，不触发发布链路；
3) 绑定后正文（含多行与链接）原样透传给 _handle_publish，作用域为工作区群；
4) _private_uid 标记的轻量消息在 _safe_reply 中改走私聊，绝不发群。
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from cdutetc import admin_session, config, llm, main, reminder_preflight, daily_digest
from cdutetc import database as db
from cdutetc.clients.qqbot import QQBotClient, QQBotMessage

NOW = datetime(2026, 11, 11, 18, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def env(monkeypatch, tmp_path):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)

    for module in (main, reminder_preflight, daily_digest):
        monkeypatch.setattr(module, "datetime", Clock)

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "private_publish.sqlite3"))
    monkeypatch.setattr(db, "_delivery_now", lambda value=None: value or NOW)
    monkeypatch.setattr(config, "BOT_PROTOCOL", "qqbot")
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"team", "new"})
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", {"team", "new"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"t1"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "ADMIN_GROUP", "team")
    monkeypatch.setattr(config, "PUBLISH_CONFIRM_ENABLED", False)

    admin_session.reset_all()
    db.init_db()
    db.set_account_label("group", "team", "集训队")
    db.set_account_label("group", "new", "集训队二群")

    api = SimpleNamespace(post_c2c_message=AsyncMock(return_value={"id": "sent-private"}),
                          post_group_message=AsyncMock(return_value={"id": "sent-group"}))
    adapter = QQBotClient("offline-app", "offline-secret")
    adapter._client = SimpleNamespace(api=api)
    bot = main.ACMBot(adapter)
    monkeypatch.setattr(llm, "_chat_once", AsyncMock(side_effect=AssertionError("私聊发布不应直连模型")))
    publish = AsyncMock()
    monkeypatch.setattr(bot, "_handle_publish", publish)

    async def send_private(content, user="admin"):
        await bot.on_private_message_create(QQBotMessage(
            None, message_type="private", user_id=user,
            msg_id="private-inbound", content=content,
        ))
        return api.post_c2c_message.call_args.kwargs["content"]

    yield SimpleNamespace(api=api, bot=bot, send_private=send_private, publish=publish)
    admin_session.reset_all()


async def test_publish_without_active_group_asks_for_binding(env):
    text = await env.send_private("/发布 蓝桥杯省赛 3月15日")
    assert "/群配置" in text
    assert env.publish.await_count == 0


async def test_publish_with_empty_body_returns_usage(env):
    admin_session.set_active_group("admin", "new")
    text = await env.send_private("/发布   ")
    assert "用法" in text and "new" in text
    assert env.publish.await_count == 0


async def test_publish_uses_active_group_and_keeps_multiline_body(env):
    admin_session.set_active_group("admin", "new")
    body = "蓝桥杯省赛 2026-03-15 09:00\n报名截止 3月1日\nhttps://example.com/contest"
    # 发布链路被替换成桩：本用例只验证作用域与正文透传
    await env.bot.on_private_message_create(QQBotMessage(
        None, message_type="private", user_id="admin",
        msg_id="private-inbound", content=f"/发布 {body}",
    ))

    assert env.publish.await_count == 1
    args = env.publish.await_args
    message, text, group_id, member_id = args.args
    assert group_id == "new" and member_id == "admin"
    assert text == body
    assert message.group_id == "new"
    assert getattr(message, "_private_uid") == "admin"


async def test_safe_reply_routes_private_marked_message_to_private(env):
    marked = SimpleNamespace(group_id="new", user_id="admin", id="1", _private_uid="admin")
    assert await env.bot._safe_reply(marked, "回执只走私聊") is True
    env.api.post_c2c_message.assert_awaited_once()
    assert env.api.post_group_message.await_count == 0
