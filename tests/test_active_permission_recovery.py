"""复现40034105：不刷重试，持久暂停，真实主动探测成功后恢复未来提醒。"""

from datetime import datetime, timedelta
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from cdutetc import config, database, main, reminder
from cdutetc.clients.onebot import OneBotClient
from cdutetc.clients.qqbot import GroupActivePermission, QQBotClient, QQBotMessage

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 7, 10, 10, 16, tzinfo=TZ)
TARGET = "2026-11-07 10:25"


class ServerError(RuntimeError):
    def __init__(self, payload):
        self.msgs = payload


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BOT_PROTOCOL", "qqbot")
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    monkeypatch.setattr(config, "CONTEST_REMIND_OFFSETS_MINUTES", (30, 15))
    monkeypatch.setattr(config, "CONTEST_REMIND_GRACE_SECONDS", 120)
    monkeypatch.setattr(config, "REMIND_OFFSET_HOURS", (1,))
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "permission.sqlite"))
    clock = SimpleNamespace(current=NOW, enabled=False)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.current
    monkeypatch.setattr(reminder, "datetime", Clock)
    monkeypatch.setattr(database, "_delivery_now", lambda value=None: value or clock.current)
    monkeypatch.setattr(database, "now_str", lambda: clock.current.strftime("%Y-%m-%d %H:%M:%S"))
    database.init_db()
    async def post(**kwargs):
        if kwargs["group_openid"] == "g1" and not kwargs.get("msg_id") and not clock.enabled:
            raise ServerError("主动消息失败, 无权限")
        return {"id": "platform-accepted"}
    api = SimpleNamespace(post_group_message=AsyncMock(side_effect=post))
    client = QQBotClient("offline-app", "offline-secret")
    client._client = SimpleNamespace(api=api)
    clock.current = NOW - timedelta(hours=1)
    cid = database.create_competition({"name": "权限测试赛", "official_time": TARGET}, status="confirmed")
    database.set_competition_notify_groups(cid, {"g1"})
    clock.current = NOW
    return SimpleNamespace(bot=main.ACMBot(client), api=api, clock=clock, client=client, cid=cid)


def current_node():
    return next(row for row in database.list_deliveries("g1") if row["scheduled_at"] == "2026-11-07 10:10:00")


async def test_reported_sdk_error_only_hits_platform_once_and_survives_restart(app):
    for seconds in (0, 30, 60):
        app.clock.current = NOW + timedelta(seconds=seconds)
        await app.bot._contest_remind_job()
    app.api.post_group_message.assert_awaited_once()
    assert current_node()["attempts"] == 1
    assert current_node()["status"] == "pending" and current_node()["sent_at"] == ""
    assert current_node()["error_code"] == "active_forbidden"
    assert database.get_group_notification_block("qqbot", "g1")["platform_code"] == 40034105
    database.init_db()
    restarted = main.ACMBot(app.client)
    await restarted._contest_remind_job()
    app.api.post_group_message.assert_awaited_once()
    assert current_node()["attempts"] == 1


async def test_block_does_not_disable_passive_status_or_another_group(app):
    await app.bot._contest_remind_job()
    message = QQBotMessage(None, message_type="group", group_id="g1", user_id="admin",
                           content="/提醒状态", msg_id="fresh-status-id", always_addressed=True)
    await app.bot.on_group_at_message_create(message)
    passive = app.api.post_group_message.await_args.kwargs
    assert passive["msg_id"] == "fresh-status-id"
    assert "40034105" in passive["content"] and "暂停" in passive["content"]
    assert "通知测试" in passive["content"]
    assert await app.bot._send_group_result("g2", "其他群正常主动通知")
    assert app.api.post_group_message.await_args.kwargs["group_openid"] == "g2"
    assert database.get_group_notification_block("qqbot", "g2") is None


async def test_blocked_general_reminders_do_not_consume_attempts(app):
    await app.bot._contest_remind_job()
    database.create_collection("g1", "本群报名", deadline=TARGET)
    database.create_collection("g2", "另一群报名", deadline=TARGET)
    await app.bot._general_remind_job()
    assert len([call for call in app.api.post_group_message.await_args_list
                if call.kwargs["group_openid"] == "g1"]) == 1
    assert not any(row["obj_type"] == "collection" for row in database.list_deliveries("g1"))
    assert database.list_deliveries("g2")[0]["status"] == "sent"


async def test_failed_manual_probe_preserves_pause_and_success_restores_live_node(app):
    await app.bot._contest_remind_job()
    failure = await app.bot._notification_test("admin", "群 g1")
    assert "40034105" in failure and "active_forbidden" in failure
    assert database.get_group_notification_block("qqbot", "g1")
    assert current_node()["attempts"] == 1
    app.clock.enabled = True
    success = await app.bot._notification_test("admin", "群 g1")
    assert "平台已接受" in success and "暂停已解除" in success
    assert "msg_id" not in app.api.post_group_message.await_args.kwargs
    assert database.get_group_notification_block("qqbot", "g1") is None
    await app.bot._contest_remind_job()
    assert current_node()["status"] == "sent" and current_node()["attempts"] == 2
    assert "开赛提醒" in app.api.post_group_message.await_args.kwargs["content"]
    previous = app.api.post_group_message.await_count
    await app.bot._contest_remind_job()
    assert app.api.post_group_message.await_count == previous


async def test_unblocking_after_grace_never_backfills_expired_reminder(app):
    await app.bot._contest_remind_job()
    app.clock.current = NOW + timedelta(minutes=3)
    app.clock.enabled = True
    assert "平台已接受" in await app.bot._notification_test("admin", "群 g1")
    previous = app.api.post_group_message.await_count
    await app.bot._contest_remind_job()
    assert current_node()["status"] == "expired"
    assert app.api.post_group_message.await_count == previous


async def test_non_admin_or_disabled_switch_cannot_bypass_pause(app, monkeypatch):
    await app.bot._contest_remind_job()
    query = AsyncMock(return_value=GroupActivePermission(allowed=True, code="ok"))
    monkeypatch.setattr(app.client, "get_group_active_permission", query)
    count = app.api.post_group_message.await_count
    assert "permission" in await app.bot._notification_test("member", "群 g1")
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", False)
    assert "disabled" in await app.bot._notification_test("admin", "群 g1")
    assert app.api.post_group_message.await_count == count
    assert database.get_group_notification_block("qqbot", "g1")
    query.assert_not_awaited()


async def test_qq_permission_pause_is_not_reused_by_onebot(app, monkeypatch):
    await app.bot._contest_remind_job()
    monkeypatch.setattr(config, "BOT_PROTOCOL", "onebot")
    client = OneBotClient("ws://127.0.0.1:3001")
    client._ws = object()
    client.call_api = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"message_id": 1}})
    assert await main.ACMBot(client)._send_group_result("g1", "切换协议后正常发送")
    client.call_api.assert_awaited_once()


async def test_transient_platform_failure_still_retries_and_is_not_permission_block(app):
    app.api.post_group_message.side_effect = [ServerError({"code": 40034100}), {"id": "accepted"}]
    await app.bot._contest_remind_job()
    assert database.get_group_notification_block("qqbot", "g1") is None
    assert current_node()["error_code"] == "rate_limited"
    app.clock.current += timedelta(seconds=30)
    await app.bot._contest_remind_job()
    assert current_node()["status"] == "sent" and current_node()["attempts"] == 2


async def test_markdown_probe_recovers_group_and_reminders_reuse_format_after_restart(app, monkeypatch):
    await app.bot._contest_remind_job()
    async def accept_markdown(**params):
        if not params.get("msg_id") and params["msg_type"] != 2:
            raise ServerError("主动消息失败, 无权限")
        return {"id": "accepted"}
    app.api.post_group_message.side_effect = accept_markdown
    query = AsyncMock(return_value=GroupActivePermission(allowed=True, code="ok"))
    monkeypatch.setattr(app.client, "get_group_active_permission", query)
    model = AsyncMock(side_effect=AssertionError("通知测试和提醒不能调用模型"))
    monkeypatch.setattr(main.llm, "_chat_once", model)
    message = QQBotMessage(None, message_type="group", group_id="g1", user_id="admin",
                           content="/通知测试 群 Markdown", msg_id="fresh-probe", always_addressed=True)
    await app.bot.on_group_at_message_create(message)
    probe, response = [call.kwargs for call in app.api.post_group_message.await_args_list[-2:]]
    assert probe["msg_type"] == 2 and "通知通道测试" in probe["markdown"]["content"]
    assert "msg_id" not in probe and "content" not in probe
    assert response["msg_id"] == "fresh-probe" and response["msg_type"] == 0
    assert "平台已接受" in response["content"] and "allow_proactive_msg=true" in response["content"]
    query.assert_awaited_once_with("g1")
    assert database.get_group_notification_block("qqbot", "g1") is None
    assert database.get_group_notification_format("qqbot", "g1") == "markdown"
    assert database.get_group_notification_format("qqbot", "g2") == "text"
    assert database.get_group_notification_format("onebot", "g1") == "text"
    database.init_db()
    restarted = main.ACMBot(app.client)
    await restarted._contest_remind_job()
    notice = app.api.post_group_message.await_args.kwargs
    assert "开赛提醒" in notice["markdown"]["content"] and notice["msg_type"] == 2
    assert "msg_id" not in notice and notice["msg_seq"] != probe["msg_seq"]
    assert current_node()["status"] == "sent" and current_node()["attempts"] == 2
    count = app.api.post_group_message.await_count
    await restarted._contest_remind_job()
    assert app.api.post_group_message.await_count == count
    model.assert_not_awaited()


async def test_failed_format_change_keeps_last_working_format_and_pause(app):
    app.clock.enabled = True
    assert "平台已接受" in await app.bot._notification_test("admin", "群 g1 Markdown")
    app.clock.enabled = False
    failure = await app.bot._notification_test("admin", "群 g1 文本")
    assert "active_forbidden" in failure and "本次格式：文本" in failure
    assert database.get_group_notification_format("qqbot", "g1") == "markdown"
    assert database.get_group_notification_block("qqbot", "g1")
    assert current_node()["attempts"] == 0


@pytest.mark.parametrize("receipt", [None, {"timestamp": "2026-11-07T10:10:16+08:00"}])
async def test_markdown_probe_without_receipt_cannot_change_format_or_resume(app, receipt):
    await app.bot._contest_remind_job()
    app.api.post_group_message.side_effect = None
    app.api.post_group_message.return_value = receipt
    answer = await app.bot._notification_test("admin", "群 g1 Markdown")
    assert "平台已接受" not in answer and "暂停已解除" not in answer
    assert database.get_group_notification_format("qqbot", "g1") == "text"
    assert database.get_group_notification_block("qqbot", "g1")


@pytest.mark.parametrize("state, expected", [
    (GroupActivePermission(allowed=False, code="ok"), "allow_proactive_msg=false"),
    (GroupActivePermission(allowed=True, code="ok"), "allow_proactive_msg=true"),
    (GroupActivePermission(code="platform_error", platform_code=11253), "暂未核实 [platform_error/11253]"),
])
async def test_platform_switch_query_cannot_override_failed_send(app, monkeypatch, state, expected):
    monkeypatch.setattr(app.client, "get_group_active_permission", AsyncMock(return_value=state))
    answer = await app.bot._notification_test("admin", "群 g1 Markdown")
    assert expected in answer and "active_forbidden" in answer
    assert "平台已接受" not in answer and "暂停已解除" not in answer
    assert database.get_group_notification_format("qqbot", "g1") == "text"
    assert database.get_group_notification_block("qqbot", "g1")


async def test_status_query_failure_does_not_hide_accepted_send(app, monkeypatch):
    app.clock.enabled = True
    query = AsyncMock(side_effect=RuntimeError("secret request body"))
    monkeypatch.setattr(app.client, "get_group_active_permission", query)
    answer = await app.bot._notification_test("admin", "群 g1 Markdown")
    assert "平台已接受" in answer and "暂未核实 [unknown]" in answer
    assert "secret request body" not in answer
    assert database.get_group_notification_format("qqbot", "g1") == "markdown"


async def test_format_selection_rejects_other_protocol_and_unallowed_target(app, monkeypatch):
    assert "target_not_allowed" in await app.bot._notification_test("admin", "群 other Markdown")
    app.api.post_group_message.assert_not_awaited()
    assert database.get_group_notification_format("qqbot", "other") == "text"
    # 能力位由客户端声明：代理不支持 markdown 时必须拒绝，业务层不再看配置协议
    monkeypatch.setattr(app.client, "supports_markdown", False)
    assert "validation" in await app.bot._notification_test("admin", "群 g1 Markdown")
    app.api.post_group_message.assert_not_awaited()


async def test_saving_format_and_unpausing_are_atomic(app):
    await app.bot._contest_remind_job()
    with database._connect() as conn:
        conn.execute("CREATE TRIGGER reject_unpause BEFORE DELETE ON group_notification_blocks"
                     " BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        database.confirm_group_notification_channel("qqbot", "g1", "markdown")
    assert database.get_group_notification_format("qqbot", "g1") == "text"
    assert database.get_group_notification_block("qqbot", "g1")
    app.clock.enabled = True
    answer = await app.bot._notification_test("admin", "群 g1 Markdown")
    assert "平台已接受" in answer and "保存失败" in answer
    assert "暂停已解除" not in answer and "已记住本群格式" not in answer
    assert database.get_group_notification_format("qqbot", "g1") == "text"
    assert database.get_group_notification_block("qqbot", "g1")
