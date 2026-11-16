"""官方群消息路由、有效回执和提醒重试的离线回归。"""

import asyncio
import importlib
import re
import sqlite3
from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from cdutetc import commands, config, database, main, reminder
from conftest import official_message

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def memory_db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    database.init_db()
    yield conn
    conn.close()


@pytest.fixture
async def bot(memory_db, monkeypatch, official_client):
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2", "g3"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "GROUP_REQUIRE_AT", False)
    monkeypatch.setattr(config, "PUBLISH_CONFIRM_ENABLED", False)
    monkeypatch.setattr(config, "NL_INTENT_ENABLED", False)
    client = main.ACMBot(official_client)
    monkeypatch.setattr(client, "_safe_reply", AsyncMock(wraps=client._safe_reply))
    return client, official_client


def _message(content="/帮助", group="g1", member="student", msg_id="incoming-id"):
    return official_message(message_type="group", msg_id=msg_id, content=content, group_id=group, user_id=member, bot_id=30001)


def _active_calls(client, api):
    replies = {(str(call.args[0].group_id), call.args[1]) for call in client._safe_reply.await_args_list}
    return [call.kwargs for call in api.api.post_group_message.await_args_list
            if (str(call.kwargs["group_openid"]), call.kwargs["content"]) not in replies]


def _reply_calls(client, api):
    replies = {(str(call.args[0].group_id), call.args[1]) for call in client._safe_reply.await_args_list}
    return [call.kwargs for call in api.api.post_group_message.await_args_list
            if (str(call.kwargs["group_openid"]), call.kwargs["content"]) in replies]


def _source_text(client, api):
    return "\n".join(call["content"] for call in _reply_calls(client, api))


def _assert_sent_ratio(text, sent, total):
    assert re.search(rf"{sent}/{total}\s*个(?:目标)?群", text), text


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
async def test_active_send_gate_never_calls_official(bot, monkeypatch, enabled, group):
    client, api = bot
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", enabled)

    assert not await client._send_group_result(group, "通知")
    api.api.post_group_message.assert_not_awaited()


@pytest.mark.parametrize("response", [{"id": "accepted"}, {"id": "sent-id"}])
async def test_active_send_requires_confirmed_response_and_correct_target(bot, response):
    client, api = bot
    api.api.post_group_message.return_value = response
    assert await client._send_group_result("g2", "跨群通知")
    api.api.post_group_message.assert_awaited_once()
    sent = api.api.post_group_message.await_args.kwargs
    assert sent["group_openid"] == "g2" and sent["content"] == "跨群通知"
    assert sent["msg_seq"] > 0 and "msg_id" not in sent



@pytest.mark.parametrize("response", [None, {}, [], "bad", {"id": ""}, {"code": 40034105}])
async def test_active_send_rejects_missing_or_error_receipts(bot, response):
    client, api = bot
    api.api.post_group_message.return_value = response
    assert not await client._send_group_result("g2", "通知")



async def test_transport_exception_returns_false_for_active_send_and_reply(bot):
    client, api = bot
    api.api.post_group_message.side_effect = RuntimeError("模拟发送失败")
    assert not await client._send_group_result("g2", "通知")
    assert await client._safe_reply(_message(), "回复") is False



@pytest.mark.parametrize("group, msg_id, expected", [
    ("unauthorized", "incoming-id", False), ("g1", None, False), ("g1", "incoming-id", True),
])
async def test_reply_requires_authorized_group_and_source_message_id(bot, group, msg_id, expected):
    client, api = bot
    assert await client._safe_reply(_message(group=group, msg_id=msg_id), "回复") is expected
    if expected:
        sent = api.api.post_group_message.await_args.kwargs
        assert sent["group_openid"] == group and sent["msg_id"] == msg_id
    else:
        api.api.post_group_message.assert_not_awaited()



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
        group_calls = [call for call in calls if call["group_openid"] == group]
        assert [call["content"] for call in group_calls] == [f"/帮助 {marker} 第一段", f"/帮助 {marker} 第二段"]
        assert [call["msg_seq"] for call in group_calls] == [1, 2]
        assert all(call["msg_id"] == ("event-a" if group == "g1" else "event-b") for call in group_calls)


async def test_source_event_merges_six_segments_within_official_reply_limit(bot, monkeypatch):
    client, api = bot

    async def six_replies(text, group_id, sender):
        return [{"group_id": group_id, "text": f"第 {index} 段"} for index in range(1, 7)]

    monkeypatch.setattr(commands, "handle_command", six_replies)
    await client.on_group_at_message_create(_message())
    assert _active_calls(client, api) == []
    replies = _reply_calls(client, api)
    assert [call["content"] for call in replies[:4]] == [f"第 {index} 段" for index in range(1, 5)]
    assert "第 5 段" in replies[4]["content"] and "第 6 段" in replies[4]["content"]
    assert all(call["group_openid"] == "g1" and call["msg_id"] == "incoming-id" for call in replies)
    assert [call["msg_seq"] for call in replies] == [1, 2, 3, 4, 5]


async def test_actual_amend_routes_to_other_groups_and_reports_partial_failure(bot):
    client, api = bot
    cid = database.create_competition({"name": "校赛"}, status="confirmed")

    async def send(**kwargs):
        if kwargs["group_openid"] == "g2":
            return None
        return {"id": "accepted"}

    api.api.post_group_message.side_effect = send
    await client.on_group_at_message_create(
        _message(f"/修正 {cid} summary=updated", member="admin")
    )

    assert database.get_competition(cid)["summary"] == "updated"
    active = _active_calls(client, api)
    assert [call["group_openid"] for call in active] == ["g2", "g3"]
    assert all("event_id" not in call and "msg_id" not in call for call in active)
    assert all(call["group_openid"] == "g1" for call in _reply_calls(client, api))
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

    async def no_active_receipt(**kwargs):
        return {"id": "accepted"} if kwargs["group_openid"] == "g1" else None

    api.api.post_group_message.side_effect = no_active_receipt
    await client.on_group_at_message_create(
        _message(f"/修正 {cid} summary=updated", member="admin")
    )

    assert [call["group_openid"] for call in _active_calls(client, api)] == ["g2", "g3"]
    text = _source_text(client, api)
    _assert_sent_ratio(text, 0, 2)
    assert "群通知未确认发送" in text
    assert "群通知已发送" not in text


async def test_publish_notifies_all_allowed_groups_and_reports_real_results(bot, monkeypatch):
    client, api = bot
    monkeypatch.setattr(config, "PUBLISH_SCOPE_TO_SOURCE", False)
    monkeypatch.setattr(main, "fetch_text", AsyncMock(return_value="比赛官网正文"))
    monkeypatch.setattr(main, "extract_contest", AsyncMock(return_value=({
        "name": "新校赛", "reg_deadline": "2026-11-10 23:59",
        "contest_time": "2026-11-20 09:00",
    }, None)))

    async def send(**kwargs):
        if kwargs["group_openid"] == "g2":
            return None
        return {"id": "accepted"}

    api.api.post_group_message.side_effect = send
    await client.on_group_at_message_create(
        _message("发布 https://example.com/contest", member="teacher")
    )

    assert len(database.list_competitions(status="confirmed")) == 1
    assert [call["group_openid"] for call in _active_calls(client, api)] == ["g1", "g2", "g3"]
    assert all("event_id" not in call for call in _active_calls(client, api))
    calls = [call.kwargs for call in api.api.post_group_message.await_args_list]
    assert [call["group_openid"] for call in calls[:-1]] == ["g1", "g2", "g3"]
    assert all(call["content"].startswith("【新比赛发布】") for call in calls[:-1])
    assert all("msg_id" not in call for call in calls[:-1])
    assert calls[-1]["msg_id"] == "incoming-id"
    assert calls[-1]["group_openid"] == "g1" and "已入库" in calls[-1]["content"]
    assert len(_reply_calls(client, api)) == 1
    text = _source_text(client, api)
    assert re.search(r"其余\s*1\s*个群未确认发送", text)
    assert "[timeout_unknown]" in text


def _use_fixed_general_reminders(monkeypatch):
    async def check(send):
        return await reminder.check_general_reminders(send, now=NOW)

    monkeypatch.setattr(main, "check_general_reminders", check)


async def test_actual_sender_failure_retries_then_marks_reminder_sent_once(bot, memory_db, monkeypatch):
    client, api = bot
    database.create_collection("g1", "报名", deadline="2026-11-01 23:59")
    _use_fixed_general_reminders(monkeypatch)
    api.api.post_group_message.side_effect = [None, {"id": "accepted"}]

    await client._general_remind_job()
    delivery = database.list_pending_deliveries()[0]
    assert delivery["attempts"] == 1 and delivery["status"] == "pending"

    await client._general_remind_job()
    assert database.list_pending_deliveries() == []
    row = memory_db.execute("SELECT status, attempts FROM reminder_deliveries").fetchone()
    assert row == ("sent", 2)
    await client._general_remind_job()
    assert api.api.post_group_message.await_count == 2
    assert [call["group_openid"] for call in _active_calls(client, api)] == ["g1", "g1"]


async def test_disabled_reminder_job_preserves_retry_budget_until_enabled(bot, memory_db, monkeypatch):
    client, api = bot
    database.create_collection("g1", "报名", deadline="2026-11-01 23:59")
    _use_fixed_general_reminders(monkeypatch)
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", False)

    for _ in range(4):
        await client._general_remind_job()

    api.api.post_group_message.assert_not_awaited()
    assert memory_db.execute("SELECT COUNT(*) FROM reminder_deliveries").fetchone()[0] == 0

    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    await client._general_remind_job()
    assert memory_db.execute("SELECT status, attempts FROM reminder_deliveries").fetchone() == ("sent", 1)
    api.api.post_group_message.assert_awaited_once()


@pytest.mark.parametrize("enabled", [False, True])
def test_reminder_status_shows_active_message_configuration(memory_db, monkeypatch, enabled):
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", enabled, raising=False)
    text = commands.cmd_reminder_status("g1", "teacher", "")[0]["text"]
    assert "主动" in text
    assert any(word in text for word in (("启用", "开启") if enabled else ("关闭", "未启用")))
