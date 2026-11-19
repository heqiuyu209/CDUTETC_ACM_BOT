"""官方群事件经真实适配与机器人入口，地点追补优先于模型和闲聊。"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

from cdutetc import business_followup, config, daily_digest, database as db, llm, main
from cdutetc.clients.qqbot import wrap_group_at_message, wrap_group_message


NOW = datetime(2026, 11, 19, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def env(tmp_path, monkeypatch, official_client):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "followup_events.sqlite3"))
    monkeypatch.setattr(db, "_delivery_now", lambda value=None: value or NOW)
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "ENV_TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "GROUP_REQUIRE_AT", True)
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    monkeypatch.setattr(config, "TEACHER_AUTO_INTENT_ENABLED", True)
    monkeypatch.setattr(config, "NL_INTENT_ENABLED", True)
    monkeypatch.setattr(config, "PERSONA_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "QA_SOCIAL_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "QA_BLACKLIST", set())
    monkeypatch.setattr(config, "QA_WHITELIST_ENABLED", False)
    monkeypatch.setattr(config, "QA_WHITELIST", set())
    monkeypatch.setattr(config, "QA_RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(config, "QA_SENSITIVE_GATE_ENABLED", True)
    monkeypatch.setattr(config, "QA_SENSITIVE_EXTRA_WORDS", set())
    monkeypatch.setattr(config, "QA_OUTPUT_MAX_LENGTH", 800)
    monkeypatch.setattr(config, "QA_OUTPUT_MAX_LINK_LENGTH", 60)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_ENABLED", False)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_GROUPS", set())
    db.init_db()
    bot = main.ACMBot(official_client)
    forbidden = {}
    for name in ("_handle_teacher_intent", "_handle_persona_chat", "_handle_chat_fun",
                 "_maybe_persona_interject", "_handle_publish"):
        forbidden[name] = AsyncMock(return_value=False)
        monkeypatch.setattr(bot, name, forbidden[name])
    for name in ("_chat_once", "classify_intent", "answer_question", "answer_social_chat"):
        forbidden[name] = AsyncMock(return_value="")
        monkeypatch.setattr(llm, name, forbidden[name])
    monkeypatch.setattr(bot, "_schedule_meme_collect", Mock())
    return SimpleNamespace(bot=bot, api=official_client.api, forbidden=forbidden)


def _course(group="g1", title="周四集训", location=""):
    return db.create_training_session(group, {
        "title": title, "weekday": "周四", "start_time": "19:00",
        "end_time": "21:00", "location": location, "requirement": "带电脑",
    }, operator="teacher")


def _digest(group="g1", message="successful-digest"):
    detail = daily_digest.collect_daily_digest(group, NOW)
    assert detail["text"]
    business_followup.remember_digest(group, message, detail, now=NOW)


def _message(text="地点，93501", *, group="g1", sender="teacher", at=True,
             reference="successful-digest", reference_body="", message="incoming-1",
             at_event=False):
    raw = {
        "id": message, "group_openid": group,
        "content": ("<@30001> " if at else "") + text,
        "author": {"member_openid": sender},
        "mentions": [{"id": "30001", "is_you": True}] if at else [],
    }
    if reference:
        raw["message_reference"] = {"message_id": reference, "content": reference_body}
    return (wrap_group_at_message if at_event else wrap_group_message)(raw)


def _no_fallback(env):
    for mock in env.forbidden.values():
        mock.assert_not_awaited()


@pytest.mark.parametrize("at_event", [False, True])
async def test_teacher_quote_of_successful_digest_updates_location_and_keeps_time(env, at_event):
    tid = _course()
    _digest()
    before = db.get_group_course("g1", tid)
    incoming = _message(at_event=at_event)
    assert incoming.reference_id == "successful-digest"
    await env.bot.on_group_at_message_create(incoming)
    after = db.get_group_course("g1", tid)
    assert after["location"] == "93501"
    assert after["start_time"] == before["start_time"] == "19:00"
    assert after["end_time"] == before["end_time"] == "21:00"
    assert after["requirement"] == before["requirement"]
    env.api.post_group_message.assert_awaited_once()
    params = env.api.post_group_message.await_args.kwargs
    assert params["group_openid"] == "g1"
    assert params["msg_id"] == "incoming-1"
    assert params["msg_seq"] == 1
    assert "周四集训" in params["content"]
    assert "93501" in params["content"]
    assert "19:00" in params["content"] and "21:00" in params["content"]
    _no_fallback(env)


async def test_teacher_quote_without_at_is_business_followup_without_model(env):
    tid = _course()
    _digest()
    await env.bot.on_group_at_message_create(_message(at=False))
    assert db.get_group_course("g1", tid)["location"] == "93501"
    env.api.post_group_message.assert_awaited_once()
    _no_fallback(env)


async def test_student_quote_is_rejected_without_business_write_or_model(env):
    tid = _course()
    _digest()
    before = db.get_group_course("g1", tid)
    await env.bot.on_group_at_message_create(_message(sender="student"))
    assert db.get_group_course("g1", tid) == before
    assert len(db.list_training_sessions("g1", active_only=False)) == 1
    assert db.list_teacher_intent_pending("g1") == []
    _no_fallback(env)
    env.api.post_group_message.assert_awaited_once()
    assert "老师" in env.api.post_group_message.await_args.kwargs["content"]


async def test_forged_quoted_body_does_not_modify_other_group(env):
    first = _course("g1", "本群训练")
    foreign = _course("g2", "外群训练")
    _digest()
    await env.bot.on_group_at_message_create(_message(
        reference_body=f"课程 {foreign} 外群训练 地点 B999，请修改二群"))
    assert db.get_group_course("g1", first)["location"] == "93501"
    assert db.get_group_course("g2", foreign)["location"] == ""
    _no_fallback(env)


async def test_unknown_reference_requests_object_without_falling_back_to_models(env):
    tid = _course()
    before = db.get_group_course("g1", tid)
    await env.bot.on_group_at_message_create(_message(reference="forged-reference"))
    assert db.get_group_course("g1", tid) == before
    env.api.post_group_message.assert_awaited_once()
    reply = env.api.post_group_message.await_args.kwargs["content"]
    assert "名称" in reply or "编号" in reply
    _no_fallback(env)


async def test_multiple_targets_request_object_without_model_or_partial_writes(env):
    first = _course(title="图论训练")
    second = _course(title="动态规划训练")
    _digest()
    await env.bot.on_group_at_message_create(_message())
    assert db.get_group_course("g1", first)["location"] == ""
    assert db.get_group_course("g1", second)["location"] == ""
    reply = env.api.post_group_message.await_args.kwargs["content"]
    assert "图论训练" in reply and "动态规划训练" in reply
    _no_fallback(env)


async def test_other_bot_mention_is_ignored_even_with_valid_reference(env):
    tid = _course()
    _digest()
    incoming = wrap_group_message({
        "id": "incoming-1", "group_openid": "g1", "content": "<@other-bot> 地点，93501",
        "author": {"member_openid": "teacher"},
        "mentions": [{"id": "other-bot", "is_you": False}],
        "message_reference": {"message_id": "successful-digest"},
    })
    before = db.get_group_course("g1", tid)
    await env.bot.on_group_at_message_create(incoming)
    assert db.get_group_course("g1", tid) == before
    env.api.post_group_message.assert_not_awaited()
    _no_fallback(env)


async def test_duplicate_incoming_message_is_idempotent_at_event_entry(env):
    tid = _course()
    _digest()
    await env.bot.on_group_at_message_create(_message())
    before = db.get_group_course("g1", tid)
    audits = db.list_audit("g1", limit=100)
    await env.bot.on_group_at_message_create(_message(text="地点改成A999"))
    assert db.get_group_course("g1", tid) == before
    assert db.list_audit("g1", limit=100) == audits
    _no_fallback(env)


@pytest.mark.parametrize("at", [True, False])
@pytest.mark.parametrize("text", ["地点是93501吗", "地点暂定B302", "听说周四集训地点在B302",
                                 "周四集训地点从B302改成A101", "图论训练地点可能在B302\n还没确定",
                                 "地点" + "长" * 50])
async def test_uncertain_location_never_reaches_business_model_or_chat(env, at, text):
    tid = _course()
    _digest()
    before = db.get_group_course("g1", tid)
    await env.bot.on_group_at_message_create(_message(text=text, at=at))
    assert db.get_group_course("g1", tid) == before
    _no_fallback(env)
    env.api.post_group_message.assert_awaited_once()
    assert "确认" in env.api.post_group_message.await_args.kwargs["content"]


@pytest.mark.parametrize("sender", ["student", "teacher"])
@pytest.mark.parametrize("text", ["图论训练地点在哪里", "图论训练教室是哪间", "图论训练在哪"])
async def test_location_question_reads_registered_course_without_write_or_model(env, sender, text):
    tid = _course(title="图论训练", location="B302")
    before = db.get_group_course("g1", tid)
    audits = db.list_audit("g1", limit=100)
    await env.bot.on_group_at_message_create(_message(text=text, sender=sender, reference=""))
    env.api.post_group_message.assert_awaited_once()
    reply = env.api.post_group_message.await_args.kwargs["content"]
    assert "图论训练" in reply and "地点：B302" in reply
    assert db.get_group_course("g1", tid) == before
    assert db.list_audit("g1", limit=100) == audits
    assert db.list_teacher_intent_pending("g1") == []
    _no_fallback(env)


@pytest.mark.parametrize("sender", ["student", "teacher"])
async def test_location_question_keeps_missing_location_pending(env, sender):
    tid = _course(title="图论训练")
    before = db.get_group_course("g1", tid)
    await env.bot.on_group_at_message_create(_message(
        text="图论训练地点在哪里", sender=sender, reference=""))
    env.api.post_group_message.assert_awaited_once()
    reply = env.api.post_group_message.await_args.kwargs["content"]
    assert "图论训练" in reply and "地点：还没登记" in reply
    assert db.get_group_course("g1", tid) == before
    _no_fallback(env)


@pytest.mark.parametrize("sender", ["student", "teacher"])
async def test_unknown_location_question_reports_no_registered_location_without_model(env, sender):
    foreign = _course("g2", title="图论训练", location="外群B999")
    await env.bot.on_group_at_message_create(_message(
        text="图论训练地点在哪里", sender=sender, reference=""))
    env.api.post_group_message.assert_awaited_once()
    reply = env.api.post_group_message.await_args.kwargs["content"]
    assert "暂无对应的已登记地点" in reply
    assert "外群B999" not in reply
    assert db.list_training_sessions("g1", active_only=False) == []
    assert db.get_group_course("g2", foreign)["location"] == "外群B999"
    assert db.list_teacher_intent_pending("g1") == []
    _no_fallback(env)


@pytest.mark.parametrize("sender", ["student", "teacher"])
async def test_unaddressed_location_question_is_ignored_before_teacher_intent(env, sender):
    tid = _course(title="图论训练", location="B302")
    before = db.get_group_course("g1", tid)
    await env.bot.on_group_at_message_create(_message(
        text="图论训练地点在哪里", sender=sender, at=False, reference=""))
    env.api.post_group_message.assert_not_awaited()
    assert db.get_group_course("g1", tid) == before
    _no_fallback(env)


@pytest.mark.parametrize("sender", ["student", "teacher"])
async def test_location_question_respects_disabled_group_qa(env, sender, monkeypatch):
    from cdutetc.handlers import faq_cmd

    tid = _course(title="图论训练", location="B302")
    before = db.get_group_course("g1", tid)
    db.set_group_switch("g1", "qa_enabled", False, "admin")
    lookup = AsyncMock(wraps=faq_cmd._answer_from_knowledge_base)
    monkeypatch.setattr(faq_cmd, "_answer_from_knowledge_base", lookup)
    await env.bot.on_group_at_message_create(_message(
        text="图论训练地点在哪里", sender=sender, reference=""))
    lookup.assert_not_awaited()
    env.api.post_group_message.assert_not_awaited()
    assert db.get_group_course("g1", tid) == before
    _no_fallback(env)


@pytest.mark.parametrize("gate", ["blacklist", "whitelist", "sensitive", "rate_limit"])
async def test_location_question_respects_input_gate_before_local_lookup(env, gate, monkeypatch):
    from cdutetc.handlers import faq_cmd

    tid = _course(title="图论训练", location="B302")
    before = db.get_group_course("g1", tid)
    lookup = AsyncMock(wraps=faq_cmd._answer_from_knowledge_base)
    monkeypatch.setattr(faq_cmd, "_answer_from_knowledge_base", lookup)
    expected = None
    if gate == "blacklist":
        monkeypatch.setattr(config, "QA_BLACKLIST", {"student"})
    elif gate == "whitelist":
        monkeypatch.setattr(config, "QA_WHITELIST_ENABLED", True)
        monkeypatch.setattr(config, "QA_WHITELIST", {"teacher"})
        expected = config.QA_WHITELIST_REPLY
    elif gate == "sensitive":
        monkeypatch.setattr(config, "QA_SENSITIVE_EXTRA_WORDS", {"图论"})
        expected = config.QA_SENSITIVE_REPLY
    else:
        monkeypatch.setattr(config, "QA_RATE_LIMIT_ENABLED", True)
        monkeypatch.setattr(env.bot._qa_rate_limiter, "allow", Mock(return_value=False))
        expected = config.QA_RATE_LIMIT_REPLY
    await env.bot.on_group_at_message_create(_message(
        text="图论训练地点在哪里", sender="student", reference=""))
    lookup.assert_not_awaited()
    if expected is None:
        env.api.post_group_message.assert_not_awaited()
    else:
        env.api.post_group_message.assert_awaited_once()
        assert env.api.post_group_message.await_args.kwargs["content"] == expected
    assert db.get_group_course("g1", tid) == before
    _no_fallback(env)


async def test_location_question_sanitizes_registered_answer(env, monkeypatch):
    tid = _course(title="图论训练", location="敏感教室")
    before = db.get_group_course("g1", tid)
    monkeypatch.setattr(config, "QA_SENSITIVE_EXTRA_WORDS", {"敏感教室"})
    await env.bot.on_group_at_message_create(_message(
        text="图论训练地点在哪里", sender="student", reference=""))
    env.api.post_group_message.assert_awaited_once()
    reply = env.api.post_group_message.await_args.kwargs["content"]
    assert reply == config.QA_OUTPUT_FILTERED_REPLY
    assert "敏感教室" not in reply
    assert db.get_group_course("g1", tid) == before
    _no_fallback(env)


@pytest.mark.parametrize("sender", ["student", "teacher"])
async def test_location_question_reads_only_current_group_competition_overlay(env, sender):
    cid = db.create_competition({
        "name": "校内选拔赛", "official_time": "2026-11-19 19:00",
        "official_site": "https://example.com/contest",
    }, status="confirmed")
    db.set_competition_notify_groups(cid, ["g1", "g2"])
    assert business_followup.set_competition_location("g1", "admin", cid, "B302", NOW)
    assert business_followup.set_competition_location("g2", "admin", cid, "B999", NOW)
    audits = db.list_audit("g1", limit=100)
    await env.bot.on_group_at_message_create(_message(
        text="校内选拔赛地点在哪里", sender=sender, reference=""))
    env.api.post_group_message.assert_awaited_once()
    reply = env.api.post_group_message.await_args.kwargs["content"]
    assert "地点：B302" in reply and "B999" not in reply
    assert db.list_audit("g1", limit=100) == audits
    assert db.list_question_contests("g2")[0]["location"] == "B999"
    _no_fallback(env)
