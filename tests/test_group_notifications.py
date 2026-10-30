"""OneBot 群消息路由、有效回执和提醒重试的离线回归。"""

import asyncio
import importlib
import re
import sqlite3
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from onebot_client import OneBotClient, OneBotMessage

import commands
import config
import database
import main
import reminder


NOW = datetime(2026, 11, 1, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def memory_db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    database.init_db()
    yield conn
    conn.close()


@pytest.fixture
async def bot(memory_db, monkeypatch, onebot_client):
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2", "g3"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True, raising=False)
    client = main.ACMBot(onebot_client)
    monkeypatch.setattr(client, "_safe_reply", AsyncMock(wraps=client._safe_reply))
    return client, onebot_client


def _message(content="/帮助", group="g1", member="student", msg_id="incoming-id"):
    return OneBotMessage({
        "post_type": "message", "message_type": "group", "message_id": msg_id,
        "raw_message": content, "group_id": group, "user_id": member, "self_id": 30001,
    })


def _active_calls(client, api):
    replies = {(str(call.args[0].group_id), call.args[1]) for call in client._safe_reply.await_args_list}
    return [call.kwargs for call in api.call_api.await_args_list
            if (str(call.kwargs["group_id"]), call.kwargs["message"]) not in replies]


def _reply_calls(client, api):
    replies = {(str(call.args[0].group_id), call.args[1]) for call in client._safe_reply.await_args_list}
    return [call.kwargs for call in api.call_api.await_args_list
            if (str(call.kwargs["group_id"]), call.kwargs["message"]) in replies]


def _source_text(client, api):
    return "\n".join(call["message"] for call in _reply_calls(client, api))


def _assert_sent_ratio(text, sent, total):
    assert re.search(rf"{sent}/{total}\s*个目标群", text), text


def test_active_message_environment_defaults_enabled_and_only_accepts_one(monkeypatch):
    try:
        with monkeypatch.context() as env:
            for value, expected in [(None, True), ("true", False), ("1", True)]:
                if value is None:
                    env.delenv("ACM_GROUP_ACTIVE_MESSAGES_ENABLED", raising=False)
                else:
                    env.setenv("ACM_GROUP_ACTIVE_MESSAGES_ENABLED", value)
                importlib.reload(config)
                assert config.GROUP_ACTIVE_MESSAGES_ENABLED is expected
    finally:
        importlib.reload(config)


@pytest.mark.parametrize("enabled, group", [(False, "g1"), (True, "unauthorized")])
async def test_active_send_gate_never_calls_onebot(bot, monkeypatch, enabled, group):
    client, api = bot
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", enabled)

    assert await client._send_group(group, "通知") is False
    api.call_api.assert_not_awaited()


@pytest.mark.parametrize("response", [
    {"status": "ok", "retcode": 0, "data": {"message_id": 12345}},
    {"status": "ok", "retcode": 0, "data": {"message_id": "sent-id"}},
])
async def test_active_send_requires_confirmed_response_and_correct_target(bot, response):
    client, api = bot
    api.call_api.return_value = response

    assert await client._send_group("g2", "跨群通知") is True
    api.call_api.assert_awaited_once_with(
        "send_group_msg", group_id="g2", message="跨群通知",
    )


@pytest.mark.parametrize("response", [
    None, {}, {"status": "ok", "retcode": 0, "data": {}},
    {"status": "ok", "retcode": 0, "data": {"message_id": 0}},
    {"status": "failed", "retcode": 400, "data": {"message_id": 12345}},
    {"status": "ok", "data": {"message_id": 12345}},
    {"status": "ok", "retcode": 0, "data": {"message_id": True}},
    {"status": "ok", "retcode": 0, "data": {"message_id": "0"}},
])
async def test_active_send_rejects_missing_or_error_receipts(bot, response):
    client, api = bot
    api.call_api.return_value = response
    assert await client._send_group("g2", "通知") is False


async def test_transport_exception_returns_false_for_active_send_and_reply(bot):
    # 使用真实 call_api，在合成 WebSocket 发送边界验证异常处理。
    api = OneBotClient("ws://127.0.0.1:3001")
    api._ws = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("模拟发送失败")))
    client = main.ACMBot(api)
    assert await client._send_group("g2", "通知") is False
    assert await client._safe_reply(_message(), "回复") is False
    assert api._pending == {}


@pytest.mark.parametrize("group, msg_id, expected", [
    ("unauthorized", "incoming-id", False), ("g1", None, True),
])
async def test_reply_checks_group_but_does_not_require_source_message_id(bot, group, msg_id, expected):
    client, api = bot
    assert await client._safe_reply(_message(group=group, msg_id=msg_id), "回复") is expected
    if expected:
        api.call_api.assert_awaited_once_with("send_group_msg", group_id=group, message="回复")
    else:
        api.call_api.assert_not_awaited()


async def test_concurrent_events_keep_both_reply_segments_in_their_own_group(bot, monkeypatch):
    client, api = bot

    async def two_replies(text, group_id, sender):
        await asyncio.sleep(0)
        return [{"group_id": group_id, "text": f"{text} 第一段"},
                {"group_id": group_id, "text": f"{text} 第二段"}]

    monkeypatch.setattr(commands, "handle_command", two_replies)
    await asyncio.gather(
        client.on_group_at_message_create(_message(content="/帮助 A", group="g1", msg_id="event-a")),
        client.on_group_at_message_create(_message(content="/帮助 B", group="g2", msg_id="event-b")),
    )
    assert _active_calls(client, api) == []
    calls = _reply_calls(client, api)
    for group, marker in [("g1", "A"), ("g2", "B")]:
        group_calls = [call for call in calls if call["group_id"] == group]
        assert [call["message"] for call in group_calls] == [f"/帮助 {marker} 第一段", f"/帮助 {marker} 第二段"]
        assert all("msg_id" not in call and "msg_seq" not in call for call in group_calls)


async def test_source_event_sends_all_six_segments_without_sdk_five_reply_limit(bot, monkeypatch):
    client, api = bot

    async def six_replies(text, group_id, sender):
        return [{"group_id": group_id, "text": f"第 {index} 段"} for index in range(1, 7)]

    monkeypatch.setattr(commands, "handle_command", six_replies)
    await client.on_group_at_message_create(_message())
    assert _active_calls(client, api) == []
    replies = _reply_calls(client, api)
    assert [call["message"] for call in replies] == [f"第 {index} 段" for index in range(1, 7)]
    assert all(call["group_id"] == "g1" and "msg_seq" not in call for call in replies)


async def test_actual_amend_routes_to_other_groups_and_reports_partial_failure(bot):
    client, api = bot
    cid = database.create_competition({"name": "校赛"}, status="confirmed")

    async def send(action, **kwargs):
        if kwargs["group_id"] == "g2":
            return None
        return {"status": "ok", "retcode": 0, "data": {"message_id": 12345}}

    api.call_api.side_effect = send
    await client.on_group_at_message_create(
        _message(f"/修正 {cid} summary=updated", member="admin")
    )

    assert database.get_competition(cid)["summary"] == "updated"
    active = _active_calls(client, api)
    assert [call["group_id"] for call in active] == ["g2", "g3"]
    assert all("event_id" not in call and "msg_id" not in call for call in active)
    assert all(call["group_id"] == "g1" for call in _reply_calls(client, api))
    text = _source_text(client, api)
    assert "已修正" in text
    _assert_sent_ratio(text, 1, 2)
    assert re.search(r"其余\s*1\s*个群未确认发送", text)
    assert "已生成版本记录并通知各群" not in text


async def test_disabled_cross_group_amend_still_replies_and_reports_unsent(bot, monkeypatch):
    client, api = bot
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", False)
    cid = database.create_competition({"name": "校赛"}, status="confirmed")

    await client.on_group_at_message_create(
        _message(f"/修正 {cid} summary=updated", member="admin")
    )

    assert database.get_competition(cid)["summary"] == "updated"
    assert _active_calls(client, api) == []
    text = _source_text(client, api)
    assert "已修正" in text
    assert "关闭" in text or "未启用" in text
    assert "通知未发送" in text
    assert "群通知已发送" not in text


async def test_amend_without_any_active_receipt_reports_unconfirmed_delivery(bot):
    client, api = bot
    cid = database.create_competition({"name": "校赛"}, status="confirmed")

    async def no_active_receipt(action, **kwargs):
        return {"status": "ok", "retcode": 0, "data": {"message_id": 12345}} if kwargs["group_id"] == "g1" else None

    api.call_api.side_effect = no_active_receipt
    await client.on_group_at_message_create(
        _message(f"/修正 {cid} summary=updated", member="admin")
    )

    assert [call["group_id"] for call in _active_calls(client, api)] == ["g2", "g3"]
    text = _source_text(client, api)
    _assert_sent_ratio(text, 0, 2)
    assert "群通知未确认发送" in text
    assert "群通知已发送" not in text


async def test_publish_notifies_all_allowed_groups_and_reports_real_results(bot, monkeypatch):
    client, api = bot
    monkeypatch.setattr(main, "fetch_text", AsyncMock(return_value="比赛官网正文"))
    monkeypatch.setattr(main, "extract_contest", AsyncMock(return_value=({
        "name": "新校赛", "reg_deadline": "2026-11-10 23:59",
        "contest_time": "2026-11-20 09:00",
    }, None)))

    async def send(action, **kwargs):
        if kwargs["group_id"] == "g2":
            return None
        return {"status": "ok", "retcode": 0, "data": {"message_id": 12345}}

    api.call_api.side_effect = send
    await client.on_group_at_message_create(
        _message("发布 https://example.com/contest", member="teacher")
    )

    assert len(database.list_competitions(status="confirmed")) == 1
    assert [call["group_id"] for call in _active_calls(client, api)] == ["g1", "g2", "g3"]
    assert all("event_id" not in call for call in _active_calls(client, api))
    calls = [call.kwargs for call in api.call_api.await_args_list]
    assert calls[0]["group_id"] == "g1" and "已记录" in calls[0]["message"]
    assert all(call["message"].startswith("【新比赛发布】") for call in calls[1:-1])
    assert all("msg_id" not in call for call in calls)
    assert calls[-1]["group_id"] == "g1" and "群通知" in calls[-1]["message"]
    assert len(_reply_calls(client, api)) == 2
    text = _source_text(client, api)
    assert "已记录" in text
    _assert_sent_ratio(text, 2, 3)
    assert re.search(r"其余\s*1\s*个群未确认发送", text)


def _use_fixed_general_reminders(monkeypatch):
    async def check(send):
        return await reminder.check_general_reminders(send, now=NOW)

    monkeypatch.setattr(main, "check_general_reminders", check)


async def test_actual_sender_failure_retries_then_marks_reminder_sent_once(bot, memory_db, monkeypatch):
    client, api = bot
    database.create_collection("g1", "报名", deadline="2026-11-01 23:59")
    _use_fixed_general_reminders(monkeypatch)
    api.call_api.side_effect = [None, {"status": "ok", "retcode": 0, "data": {"message_id": 12345}}]

    await client._remind_job()
    delivery = database.list_pending_deliveries()[0]
    assert delivery["attempts"] == 1 and delivery["status"] == "pending"

    await client._remind_job()
    assert database.list_pending_deliveries() == []
    row = memory_db.execute("SELECT status, attempts FROM reminder_deliveries").fetchone()
    assert row == ("sent", 2)
    await client._remind_job()
    assert api.call_api.await_count == 2
    assert [call["group_id"] for call in _active_calls(client, api)] == ["g1", "g1"]


async def test_disabled_reminder_job_preserves_retry_budget_until_enabled(bot, memory_db, monkeypatch):
    client, api = bot
    database.create_collection("g1", "报名", deadline="2026-11-01 23:59")
    _use_fixed_general_reminders(monkeypatch)
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", False)

    for _ in range(4):
        await client._remind_job()

    api.call_api.assert_not_awaited()
    assert memory_db.execute("SELECT COUNT(*) FROM reminder_deliveries").fetchone()[0] == 0

    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    await client._remind_job()
    assert memory_db.execute("SELECT status, attempts FROM reminder_deliveries").fetchone() == ("sent", 1)
    api.call_api.assert_awaited_once()


@pytest.mark.parametrize("enabled", [False, True])
def test_reminder_status_shows_active_message_configuration(memory_db, monkeypatch, enabled):
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", enabled, raising=False)
    text = commands.cmd_reminder_status("g1", "teacher", "")[0]["text"]
    assert "主动" in text
    assert any(word in text for word in (("启用", "开启") if enabled else ("关闭", "未启用")))
