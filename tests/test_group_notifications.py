"""真实群消息路由和 SDK 发送参数的离线回归，不发送真实 QQ 消息。"""

import asyncio
import importlib
import json
import re
import sqlite3
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from botpy.message import GroupMessage

import commands
import config
import database
import main
import llm
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
async def bot(memory_db, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2", "g3"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True, raising=False)
    client = main.ACMBot()
    api = SimpleNamespace(post_group_message=AsyncMock(return_value={"id": "sent-id"}))
    monkeypatch.setattr(client, "api", api)
    return client, api


def _message(api, content="/帮助", group="g1", member="student", msg_id="incoming-id"):
    return GroupMessage(api=api, event_id="incoming-event", data={
        "id": msg_id,
        "content": content,
        "group_openid": group,
        "author": {"member_openid": member},
    })


def _active_calls(api):
    return [call.kwargs for call in api.post_group_message.await_args_list if "msg_id" not in call.kwargs]


def _reply_calls(api):
    return [call.kwargs for call in api.post_group_message.await_args_list if "msg_id" in call.kwargs]


def _source_text(api):
    return "\n".join(call["content"] for call in _reply_calls(api))


def _assert_sent_ratio(text, sent, total):
    assert re.search(rf"{sent}/{total}\s*个目标群", text), text


def test_active_message_environment_requires_explicit_one(monkeypatch):
    try:
        with monkeypatch.context() as env:
            for value, expected in [(None, False), ("true", False), ("1", True)]:
                if value is None:
                    env.delenv("ACM_GROUP_ACTIVE_MESSAGES_ENABLED", raising=False)
                else:
                    env.setenv("ACM_GROUP_ACTIVE_MESSAGES_ENABLED", value)
                importlib.reload(config)
                assert config.GROUP_ACTIVE_MESSAGES_ENABLED is expected
    finally:
        importlib.reload(config)


@pytest.mark.parametrize("enabled, group", [(False, "g1"), (True, "unauthorized")])
async def test_active_send_gate_never_calls_sdk(bot, monkeypatch, enabled, group):
    client, api = bot
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", enabled)

    assert await client._send_group(group, "通知") is False
    api.post_group_message.assert_not_awaited()


@pytest.mark.parametrize("response", [{"id": "sent-id"}, {"id": "sent-id", "code": 0}])
async def test_active_send_requires_confirmed_response_and_correct_target(bot, response):
    client, api = bot
    api.post_group_message.return_value = response

    assert await client._send_group("g2", "跨群通知") is True
    api.post_group_message.assert_awaited_once_with(
        group_openid="g2", msg_type=0, content="跨群通知",
    )


@pytest.mark.parametrize("response", [
    None, {}, {"id": ""}, {"id": 123}, {"id": "sent-id", "code": 400},
])
async def test_active_send_rejects_missing_or_error_receipts(bot, response):
    client, api = bot
    api.post_group_message.return_value = response
    assert await client._send_group("g2", "通知") is False


async def test_sdk_exception_returns_false_for_active_send_and_reply(bot):
    client, api = bot
    api.post_group_message.side_effect = RuntimeError("模拟平台拒绝")

    assert await client._send_group("g2", "通知") is False
    assert await client._safe_reply(_message(api), "回复") is False


@pytest.mark.parametrize("group, msg_id", [("unauthorized", "incoming-id"), ("g1", None)])
async def test_reply_rejects_unauthorized_group_or_missing_source_id(bot, group, msg_id):
    client, api = bot

    assert await client._safe_reply(_message(api, group=group, msg_id=msg_id), "回复") is False
    api.post_group_message.assert_not_awaited()


async def test_each_source_event_has_independent_incrementing_reply_sequences(bot, monkeypatch):
    client, api = bot

    async def two_replies(text, group_id, sender):
        await asyncio.sleep(0)
        return [
            {"group_id": group_id, "text": "第一段"},
            {"group_id": group_id, "text": "第二段"},
        ]

    monkeypatch.setattr(commands, "handle_command", two_replies)
    await asyncio.gather(
        client.on_group_at_message_create(_message(api, msg_id="event-a")),
        client.on_group_at_message_create(_message(api, msg_id="event-b")),
    )

    assert _active_calls(api) == []
    calls = _reply_calls(api)
    for event_id in ("event-a", "event-b"):
        event_calls = [call for call in calls if call["msg_id"] == event_id]
        assert [call["msg_seq"] for call in event_calls] == [1, 2]
        assert all(call["group_openid"] == "g1" and call["msg_type"] == 0 for call in event_calls)


async def test_source_event_stops_sdk_replies_after_five_segments(bot, monkeypatch):
    client, api = bot

    async def six_replies(text, group_id, sender):
        return [{"group_id": group_id, "text": f"第 {index} 段"} for index in range(1, 7)]

    monkeypatch.setattr(commands, "handle_command", six_replies)
    await client.on_group_at_message_create(_message(api))

    assert _active_calls(api) == []
    replies = _reply_calls(api)
    assert [call["msg_seq"] for call in replies] == [1, 2, 3, 4, 5]
    assert [call["content"] for call in replies] == [f"第 {index} 段" for index in range(1, 6)]


async def test_actual_amend_routes_to_other_groups_and_reports_partial_failure(bot):
    client, api = bot
    cid = database.create_competition({"name": "校赛"}, status="confirmed")

    async def send(**kwargs):
        if "msg_id" not in kwargs and kwargs["group_openid"] == "g2":
            raise RuntimeError("模拟群 g2 发送失败")
        return {"id": "sent-id"}

    api.post_group_message.side_effect = send
    await client.on_group_at_message_create(
        _message(api, f"/修正 {cid} summary=updated", member="admin")
    )

    assert database.get_competition(cid)["summary"] == "updated"
    active = _active_calls(api)
    assert [call["group_openid"] for call in active] == ["g2", "g3"]
    assert all("event_id" not in call and "msg_id" not in call for call in active)
    assert all(call["group_openid"] == "g1" for call in _reply_calls(api))
    text = _source_text(api)
    assert "已修正" in text
    _assert_sent_ratio(text, 1, 2)
    assert re.search(r"其余\s*1\s*个群未确认发送", text)
    assert "已生成版本记录并通知各群" not in text


async def test_disabled_cross_group_amend_still_replies_and_reports_unsent(bot, monkeypatch):
    client, api = bot
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", False)
    cid = database.create_competition({"name": "校赛"}, status="confirmed")

    await client.on_group_at_message_create(
        _message(api, f"/修正 {cid} summary=updated", member="admin")
    )

    assert database.get_competition(cid)["summary"] == "updated"
    assert _active_calls(api) == []
    text = _source_text(api)
    assert "已修正" in text
    assert "关闭" in text or "未启用" in text
    assert "通知未发送" in text
    assert "群通知已发送" not in text


async def test_amend_without_any_active_receipt_reports_unconfirmed_delivery(bot):
    client, api = bot
    cid = database.create_competition({"name": "校赛"}, status="confirmed")

    async def no_active_receipt(**kwargs):
        return {"id": "reply-id"} if "msg_id" in kwargs else None

    api.post_group_message.side_effect = no_active_receipt
    await client.on_group_at_message_create(
        _message(api, f"/修正 {cid} summary=updated", member="admin")
    )

    assert [call["group_openid"] for call in _active_calls(api)] == ["g2", "g3"]
    text = _source_text(api)
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

    async def send(**kwargs):
        if "msg_id" not in kwargs and kwargs["group_openid"] == "g2":
            raise RuntimeError("模拟一个目标群失败")
        return {"id": "sent-id"}

    api.post_group_message.side_effect = send
    await client.on_group_at_message_create(
        _message(api, "发布 https://example.com/contest", member="teacher")
    )

    assert len(database.list_competitions(status="confirmed")) == 1
    assert [call["group_openid"] for call in _active_calls(api)] == ["g1", "g2", "g3"]
    assert all("event_id" not in call for call in _active_calls(api))
    calls = [call.kwargs for call in api.post_group_message.await_args_list]
    assert calls[0]["msg_id"] == "incoming-id"
    assert calls[0]["group_openid"] == "g1" and "已记录" in calls[0]["content"]
    assert all("msg_id" not in call for call in calls[1:-1])
    assert calls[-1]["msg_id"] == "incoming-id"
    assert calls[-1]["group_openid"] == "g1" and "群通知" in calls[-1]["content"]
    assert [call["msg_seq"] for call in _reply_calls(api)] == [1, 2]
    text = _source_text(api)
    assert "已记录" in text
    _assert_sent_ratio(text, 2, 3)
    assert re.search(r"其余\s*1\s*个群未确认发送", text)


@pytest.mark.parametrize("official_aliases", [False, True])
async def test_sdk_publish_twice_preserves_all_extracted_fields_without_second_notification(bot, monkeypatch, official_aliases):
    client, api = bot
    url = "https://example.com/contest"
    extracted = {
        "name": "完整校赛", "edition": "2026", "reg_start": "2026-11-01 08:00",
        "reg_deadline": "2026-11-10 23:59", "contest_time": "2026-11-20 09:00",
        "tracks": "团队赛", "raw_note": "规则原文", "device_req": "自带电脑",
        "summary": "官方摘要", "official_site": "https://invented.example.com/wrong",
    }
    if official_aliases:
        for old, canonical in (("reg_start", "official_reg_start"),
                               ("reg_deadline", "official_reg_deadline"),
                               ("contest_time", "official_time"), ("raw_note", "official_rules")):
            extracted[canonical] = extracted.pop(old)
    monkeypatch.setattr(main, "fetch_text", AsyncMock(return_value="官网正文"))
    monkeypatch.setattr(llm, "_chat_once", AsyncMock(return_value=json.dumps(extracted, ensure_ascii=False)))
    await client.on_group_at_message_create(_message(api, f"发布 {url}", member="teacher", msg_id="first"))
    record = database.list_competitions(status="confirmed")[0]
    expected = {
        "edition": "2026", "official_reg_start": "2026-11-01 08:00", "tracks": "团队赛",
        "official_rules": "规则原文", "device_req": "自带电脑", "summary": "官方摘要",
        "official_site": url,
    }
    for field, value in expected.items():
        assert record[field] == value
    first_calls = list(_active_calls(api))
    assert [call["group_openid"] for call in first_calls] == ["g1", "g2", "g3"]
    assert all("2026-11-10 23:59" in call["content"] and "2026-11-20 09:00" in call["content"]
               for call in first_calls)

    await client.on_group_at_message_create(_message(api, f"发布 {url}", member="teacher", msg_id="second"))
    assert database.list_competitions(status="confirmed") == [record]
    assert len(database.list_sources(record["id"])) == 1
    assert len(database.list_competition_versions(record["id"])) == 1
    assert _active_calls(api) == first_calls
    second_replies = [call for call in _reply_calls(api) if call["msg_id"] == "second"]
    assert len(second_replies) == 1
    assert "已存档" in second_replies[0]["content"]
    assert second_replies[0]["msg_seq"] == 1


def _use_fixed_general_reminders(monkeypatch):
    async def check(send):
        return await reminder.check_general_reminders(send, now=NOW)

    monkeypatch.setattr(main, "check_general_reminders", check)


async def test_actual_sender_failure_retries_then_marks_reminder_sent_once(bot, memory_db, monkeypatch):
    client, api = bot
    database.create_collection("g1", "报名", deadline="2026-11-01 23:59")
    _use_fixed_general_reminders(monkeypatch)
    api.post_group_message.side_effect = [RuntimeError("模拟发送失败"), {"id": "sent-id"}]

    await client._remind_job()
    delivery = database.list_pending_deliveries()[0]
    assert delivery["attempts"] == 1 and delivery["status"] == "pending"

    await client._remind_job()
    assert database.list_pending_deliveries() == []
    row = memory_db.execute("SELECT status, attempts FROM reminder_deliveries").fetchone()
    assert row == ("sent", 2)
    await client._remind_job()
    assert api.post_group_message.await_count == 2
    assert [call["group_openid"] for call in _active_calls(api)] == ["g1", "g1"]


async def test_disabled_reminder_job_preserves_retry_budget_until_enabled(bot, memory_db, monkeypatch):
    client, api = bot
    database.create_collection("g1", "报名", deadline="2026-11-01 23:59")
    _use_fixed_general_reminders(monkeypatch)
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", False)

    for _ in range(4):
        await client._remind_job()

    api.post_group_message.assert_not_awaited()
    assert memory_db.execute("SELECT COUNT(*) FROM reminder_deliveries").fetchone()[0] == 0

    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    await client._remind_job()
    assert memory_db.execute("SELECT status, attempts FROM reminder_deliveries").fetchone() == ("sent", 1)
    api.post_group_message.assert_awaited_once()


@pytest.mark.parametrize("enabled", [False, True])
def test_reminder_status_shows_active_message_configuration(memory_db, monkeypatch, enabled):
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", enabled, raising=False)
    text = commands.cmd_reminder_status("g1", "teacher", "")[0]["text"]
    assert "主动" in text
    assert any(word in text for word in (("启用", "开启") if enabled else ("关闭", "未启用")))
