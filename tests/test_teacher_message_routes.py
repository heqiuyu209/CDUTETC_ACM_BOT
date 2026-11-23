"""老师通知通过真实官方适配、群路由、本地时间校验及临时数据库落库。"""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from cdutetc import config, database as db, llm, main, teacher_intent
from cdutetc.clients import qqbot

NOW = datetime(2026, 11, 23, 9)


@pytest.fixture
def app(tmp_path, monkeypatch, official_client):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "teacher-routing.sqlite3"))
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher", "other"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "GROUP_REQUIRE_AT", True)
    monkeypatch.setattr(config, "TEACHER_AUTO_INTENT_ENABLED", True)
    monkeypatch.setattr(config, "NL_INTENT_ENABLED", False)
    monkeypatch.setattr(config, "QA_BLACKLIST", set())
    monkeypatch.setattr(config, "QA_WHITELIST_ENABLED", False)
    monkeypatch.setattr(config, "QA_RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(config, "NOTICE_UNAUTHORIZED_GROUP", False)
    monkeypatch.setattr(config, "MEME_ENABLED", False)
    monkeypatch.setattr(config, "ECHO_ENABLED", False)
    monkeypatch.setattr(config, "PERSONA_CHAT_ENABLED", False)
    monkeypatch.setattr(teacher_intent, "_now", lambda now=None: now or NOW)
    model = AsyncMock(return_value=json.dumps({
        "intent": "ONE_OFF_SESSION", "confidence": "high", "title": "DP专题",
        "date": "2027-06-15", "start_time": "03:00", "end_time": "18:00", "location": "B302",
    }))
    monkeypatch.setattr(llm, "_chat_once", model)
    db.init_db()
    db.set_group_teachers("g1", {"teacher"}, operator="admin")
    bot = main.ACMBot(official_client)
    monkeypatch.setattr(bot, "_schedule_meme_collect", Mock())
    return SimpleNamespace(bot=bot, client=official_client, model=model, monkeypatch=monkeypatch)


def _message(text="明天下午三点在B302讲DP专题", *, target=None, true_at=False,
             user="teacher", group="g1", mid="notice"):
    raw = {"id": mid, "group_openid": group, "content": (f"<@{target}> " if target else "") + text,
           "author": {"member_openid": user},
           "mentions": [] if target is None else [{"id": target, "is_you": true_at}]}
    return qqbot.wrap_group_at_message(raw) if true_at else qqbot.wrap_group_message(raw)


@pytest.mark.parametrize("target,true_at", [(None, False), ("bot", True), ("student", False), ("all", False)])
async def test_teacher_notice_is_saved_with_or_without_mentions(app, target, true_at):
    await app.client._receive_group_message(_message(target=target, true_at=true_at))
    session = db.list_training_sessions("g1", active_only=False)[0]
    assert (session["effective_start"], session["start_time"], session["end_time"]) == (
        "2026-11-24 00:00", "15:00", "")
    app.model.assert_awaited_once()
    app.client.api.post_group_message.assert_awaited_once()
    assert db.list_unanswered("g1") == []
    assert db.list_training_sessions("g2") == []


@pytest.mark.parametrize("user,group,enabled", [("member", "g1", True), ("other", "g1", True),
                                                ("teacher", "foreign", True), ("teacher", "g1", False)])
async def test_unauthorized_or_disabled_ambient_notice_does_not_call_model(app, user, group, enabled):
    app.monkeypatch.setattr(config, "TEACHER_AUTO_INTENT_ENABLED", enabled)
    await app.client._receive_group_message(_message(target="all", user=user, group=group))
    app.model.assert_not_awaited()
    assert db.list_training_sessions("g1") == []
    app.client.api.post_group_message.assert_not_awaited()


@pytest.mark.parametrize("text", ["明天几点上课？", "/帮助", "发布 明天下午三点DP比赛"])
async def test_other_member_targeted_questions_and_commands_stay_ignored(app, text):
    await app.client._receive_group_message(_message(text, target="student"))
    app.model.assert_not_awaited()
    app.client.api.post_group_message.assert_not_awaited()
    assert db.list_training_sessions("g1") == []


async def test_teacher_at_question_continues_to_read_only_route_without_classifier(app):
    answer = AsyncMock(return_value=[{"group_id": "g1", "text": "已登记课程查询"}])
    app.monkeypatch.setattr(main.commands, "handle_non_command_message", answer)
    await app.client._receive_group_message(_message("明天几点上课？", target="bot", true_at=True))
    app.model.assert_not_awaited()
    answer.assert_awaited_once()
    assert db.list_training_sessions("g1") == []


async def test_teacher_local_name_call_keeps_zero_token_reply(app):
    await app.client._receive_group_message(_message("雄姐", target="bot", true_at=True))
    app.model.assert_not_awaited()
    assert "雄姐" in app.client.api.post_group_message.await_args.kwargs["content"]


@pytest.mark.parametrize("parallel", [False, True])
async def test_full_notice_then_real_at_has_one_model_call_and_one_write(app, parallel):
    full = _message(target="bot")
    at = _message(target="bot", true_at=True)
    if parallel:
        await asyncio.gather(app.client._receive_group_message(full),
                             app.client._receive_group_message(at),
                             app.client._receive_group_message(_message(target="bot", true_at=True)))
    else:
        await app.client._receive_group_message(full)
        await app.client._receive_group_message(at)
    app.model.assert_awaited_once()
    assert len(db.list_training_sessions("g1", active_only=False)) == 1
    app.client.api.post_group_message.assert_awaited_once()
    assert app.client._pending_group_messages == {}


async def test_teacher_question_full_then_at_can_upgrade_to_read_only_answer(app):
    answer = AsyncMock(return_value=[{"group_id": "g1", "text": "已登记课程查询"}])
    app.monkeypatch.setattr(main.commands, "handle_non_command_message", answer)
    await app.client._receive_group_message(_message("明天几点上课？", target="bot"))
    await app.client._receive_group_message(_message("明天几点上课？", target="bot", true_at=True))
    app.model.assert_not_awaited()
    answer.assert_awaited_once()
    app.client.api.post_group_message.assert_awaited_once()


async def test_saved_notice_closes_upgrade_even_when_receipt_raises(app):
    app.monkeypatch.setattr(app.bot, "_safe_reply", AsyncMock(side_effect=RuntimeError("offline failure")))
    await app.client._receive_group_message(_message(target="bot"))
    await app.client._receive_group_message(_message(target="bot", true_at=True))
    assert len(db.list_training_sessions("g1", active_only=False)) == 1
    app.model.assert_awaited_once()
    assert app.client._pending_group_messages == {}


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_adapter_completion_unblocks_duplicate_after_exception_or_cancellation(failure):
    client = qqbot.QQBotClient("offline", "offline")
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handle(message):
        calls.append(message.always_addressed)
        if not message.always_addressed:
            entered.set()
            await release.wait()
            raise failure()

    client.group_msg_handler = handle
    first = asyncio.create_task(client._receive_group_message(_message(target="bot")))
    await entered.wait()
    second = asyncio.create_task(client._receive_group_message(_message(target="bot", true_at=True)))
    release.set()
    outcomes = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 1)
    if failure is asyncio.CancelledError:
        assert isinstance(outcomes[0], asyncio.CancelledError)
    assert calls == [False, True]
    assert client._pending_group_messages == {}


async def test_adapter_capacity_wait_rechecks_same_id_before_reserving(monkeypatch):
    monkeypatch.setattr(qqbot, "GROUP_MSG_DEDUP_LIMIT", 1)
    client = qqbot.QQBotClient("offline", "offline")
    entered, release, duplicate_started, duplicate_release = (asyncio.Event() for _ in range(4))
    effects = []

    async def handle(message):
        if message.id == "occupied":
            entered.set()
            await release.wait()
        else:
            effects.append(message.id)
            duplicate_started.set()
            await duplicate_release.wait()
            message.business_consumed = True

    client.group_msg_handler = handle
    occupied = asyncio.create_task(client._receive_group_message(_message(mid="occupied")))
    await entered.wait()
    full = asyncio.create_task(client._receive_group_message(_message(target="bot")))
    at = asyncio.create_task(client._receive_group_message(_message(target="bot", true_at=True)))
    await asyncio.sleep(0)
    release.set()
    await duplicate_started.wait()
    assert len(client._pending_group_messages) <= 1
    duplicate_release.set()
    await asyncio.wait_for(asyncio.gather(occupied, full, at), 1)
    assert effects == ["notice"]
    assert client._pending_group_messages == {}


@pytest.mark.parametrize("text,date,start,end,intent", [
    ("明天下午三点在机房集训", "2026-11-24", "15:00", "", "ONE_OFF_SESSION"),
    ("下周三晚上七点讲DP", "2026-12-02", "19:00", "", "ONE_OFF_SESSION"),
    ("本周三晚上七点讲DP", "2026-11-25", "19:00", "", "ONE_OFF_SESSION"),
    ("每周三19-21点讲图论", "", "19:00", "21:00", "RECURRING_COURSE"),
    ("每周三19—21时讲图论", "", "19:00", "21:00", "RECURRING_COURSE"),
    ("每周三19:00开始讲图论", "", "19:00", "", "RECURRING_COURSE"),
    ("每星期三晚七点半到九点讲图论", "", "19:30", "21:00", "RECURRING_COURSE"),
    ("明晚七点到九点讲DP", "2026-11-24", "19:00", "21:00", "ONE_OFF_SESSION"),
    ("2026年11月25日晚上七点讲DP", "2026-11-25", "19:00", "", "ONE_OFF_SESSION"),
])
def test_schedule_uses_declared_dates_clocks_and_recurrence(text, date, start, end, intent):
    slots = teacher_intent._declared_schedule(text, {
        "intent": "RECURRING_COURSE", "confidence": "high", "date": "2027-01-01",
        "weekday": "周一", "start_time": "03:00", "end_time": "05:00",
    }, NOW)
    assert (slots["date"], slots["start_time"], slots["end_time"], slots["intent"]) == (date, start, end, intent)
    assert slots["confidence"] == "high"


@pytest.mark.parametrize("text", ["明天周三晚上七点集训", "2026年11月25日周五晚上七点集训",
                                  "明天或者后天晚上七点集训", "下周找个晚上讲DP", "2026年2月30日19点集训",
                                  "晚上七点集训", "每周三晚上七点，下周开始讲图论",
                                  "11月30日起每周三晚上七点集训", "明天晚上12点到2点集训",
                                  "明天下午3点-上午5点集训", "明天下午五点到三点集训"])
async def test_ambiguous_or_missing_date_cannot_be_invented_by_model(app, text):
    await app.client._receive_group_message(_message(text))
    assert db.list_training_sessions("g1") == []
    pending = db.list_teacher_intent_pending("g1")
    assert len(pending) == 1
    assert pending[0]["missing"]


@pytest.mark.parametrize("text", ["@全体成员明天下午三点机房集训", "@全体成员 明天下午三点机房集训",
                                  "<@all>明天下午三点机房集训"])
def test_broadcast_mention_does_not_swallow_notice(text):
    assert teacher_intent.normalize_text(text) == "明天下午三点机房集训"
    assert teacher_intent.should_call_llm(text)


async def test_one_off_course_reschedule_moves_effective_window_and_drops_old_nodes(app):
    tid = db.create_training_session("g1", {"title": "DP专题", "weekday": "周二", "start_time": "15:00",
        "effective_start": "2026-11-24 00:00", "effective_end": "2026-11-24 23:59"}, operator="teacher")
    db.ensure_delivery("g1", "training_session", tid, "start_time", "2026-11-24 15:00", 1)
    app.model.return_value = json.dumps({"intent": "UPDATE_CANCEL", "confidence": "high",
        "target_id": tid, "target_type": "training_session", "date": "2030-01-01", "start_time": "03:00"})
    await app.client._receive_group_message(_message("DP专题改到下周三下午四点"))
    course = db.get_group_course("g1", tid)
    assert (course["effective_start"], course["effective_end"], course["weekday"], course["start_time"]) == (
        "2026-12-02 00:00", "2026-12-02 23:59", "周三", "16:00")
    assert db.list_deliveries("g1", statuses=("pending",)) == []


@pytest.mark.parametrize("text", ["今天课推迟半小时", "课程改到下周待定"])
def test_underspecified_reschedule_does_not_claim_success(text):
    slots = teacher_intent._declared_schedule(text, {"intent": "UPDATE_CANCEL", "confidence": "high",
        "target_id": 1, "title": "课", "start_time": "19:00"}, NOW)
    assert slots["confidence"] == "medium"
    assert "start_time" in slots["missing"]


def test_time_only_update_keeps_original_weekday_without_requiring_new_date():
    slots = teacher_intent._declared_schedule("周三课时间改为20点", {
        "intent": "UPDATE_CANCEL", "confidence": "high", "target_id": 1}, NOW)
    assert (slots["weekday"], slots["start_time"], slots["confidence"], slots["missing"]) == (
        "周三", "20:00", "high", [])


@pytest.mark.parametrize("cancel", ["false", "true", 1, False, None])
def test_model_cancel_requires_true_boolean(cancel):
    parsed = teacher_intent.parse_result(json.dumps({"intent": "UPDATE_CANCEL", "confidence": "high", "cancel": cancel}))
    assert parsed["cancel"] is False


async def test_addressed_teacher_notice_checks_rate_before_model(app):
    app.monkeypatch.setattr(config, "QA_RATE_LIMIT_ENABLED", True)
    limiter = Mock(return_value=False)
    app.monkeypatch.setattr(app.bot._qa_rate_limiter, "allow", limiter)
    await app.client._receive_group_message(_message(target="bot", true_at=True))
    limiter.assert_called_once_with("teacher")
    app.model.assert_not_awaited()
    assert db.list_training_sessions("g1", active_only=False) == []


async def test_ignored_teacher_statement_uses_rate_budget_once_then_falls_back(app):
    app.monkeypatch.setattr(config, "QA_RATE_LIMIT_ENABLED", True)
    limiter = Mock(return_value=True)
    app.monkeypatch.setattr(app.bot._qa_rate_limiter, "allow", limiter)
    app.model.return_value = '{"intent":"CHAT_IGNORE","confidence":"high"}'
    answer = AsyncMock(return_value=[{"group_id": "g1", "text": "收到"}])
    app.monkeypatch.setattr(main.commands, "handle_non_command_message", answer)
    await app.client._receive_group_message(_message("昨晚比赛打得不错，下周继续保持", target="bot", true_at=True))
    limiter.assert_called_once_with("teacher")
    app.model.assert_awaited_once()
    answer.assert_awaited_once()


@pytest.mark.parametrize("text", ["今天好开心", "明天也要开心", "你好", "今天谢谢你啦"])
async def test_teacher_date_word_banter_does_not_call_intent_model(app, text):
    await app.client._receive_group_message(_message(text, target="bot", true_at=True))
    app.model.assert_not_awaited()
    assert db.list_teacher_intent_pending("g1") == []
    assert db.list_training_sessions("g1", active_only=False) == []


async def test_other_target_location_followup_does_not_reach_teacher_pipeline(app):
    pipeline = AsyncMock()
    app.monkeypatch.setattr(app.bot, "_handle_teacher_intent", pipeline)
    await app.client._receive_group_message(_message("地点，93501", target="other-bot"))
    pipeline.assert_not_awaited()
    app.model.assert_not_awaited()
    app.client.api.post_group_message.assert_not_awaited()


async def test_timed_natural_contest_keeps_zero_model_review_before_confirmation(app):
    app.monkeypatch.setattr(config, "NL_INTENT_ENABLED", True)
    app.monkeypatch.setattr(config, "PUBLISH_CONFIRM_ENABLED", True)
    app.monkeypatch.setattr(config, "PUBLISH_SCOPE_TO_SOURCE", True)
    app.model.side_effect = AssertionError("明确比赛开始陈述应保留本地发布审批")
    pipeline = AsyncMock()
    app.monkeypatch.setattr(app.bot, "_handle_teacher_intent", pipeline)
    await app.client._receive_group_message(_message("2026年12月2号下午三点，ACM算法比赛正式开始",
                                                   target="bot", true_at=True))
    pending = db.list_pending_publications()
    assert len(pending) == 1
    assert pending[0]["official_time"] == "2026-12-02 15:00"
    assert db.list_competitions() == []
    pipeline.assert_not_awaited()
    app.model.assert_not_awaited()


def test_registration_deadline_remains_a_business_anchor():
    assert teacher_intent.should_call_llm("报名截止下周三")
    assert teacher_intent.should_call_llm("报名明天结束")
