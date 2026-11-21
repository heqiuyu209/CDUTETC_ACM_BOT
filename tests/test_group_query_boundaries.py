"""群问答的定义、安排与所问字段边界，不靠相似标题猜测事实。"""

from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from cdutetc import business_followup, commands, config, database as db, llm
from cdutetc.handlers import faq_cmd


NOW = datetime(2026, 11, 21, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "query_boundaries.sqlite3"))
    monkeypatch.setattr(db, "_delivery_now", lambda value=None: value or NOW)
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "QA_FAQ_LLM_ENABLED", False)
    monkeypatch.setattr(config, "QA_MEMBER_KB_ONLY", True)
    monkeypatch.setattr(config, "QA_MEMBER_CONTEXT_ANSWER", False)
    monkeypatch.setattr(config, "QA_SOCIAL_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_GROUPS", set())
    model = AsyncMock(side_effect=AssertionError("定义或明确业务事实不应调用模型"))
    monkeypatch.setattr(llm, "_chat_once", model)
    monkeypatch.setattr(llm, "answer_question", model)
    db.init_db()
    return model


def _competition(name="ACM排位赛", *, location="B302", time="2026-11-22 19:00",
                 deadline="2026-11-22 18:00", edition="第一场"):
    cid = db.create_competition({"name": name, "edition": edition, "official_time": time,
                                 "official_reg_deadline": deadline}, status="confirmed")
    assert business_followup.set_competition_location("g1", "admin", cid, location, NOW)
    return cid


def _course(title="ACM训练课", *, location="C201", weekday="周六", start="19:00"):
    return db.create_training_session("g1", {"title": title, "weekday": weekday,
                                             "start_time": start, "end_time": "21:00",
                                             "location": location}, "admin")


@pytest.mark.parametrize("staff", [False, True])
@pytest.mark.parametrize("question", ["ACM集训队是什么", "ACM 集训队是什么？", "什么是ACM集训队", "集训队是啥"])
async def test_team_definition_is_local_and_does_not_recall_similar_contest_titles(env, staff, question):
    _competition()
    _competition("ACM排名赛", location="A101")
    _course()
    result = await commands.handle_non_command_message(
        question, "g1", "teacher" if staff else "member", staff=staff)
    answer = result[0]["text"]
    assert "通常" in answer and "算法和程序设计" in answer
    assert "ICPC" in answer and "CCPC" in answer
    assert "查询课程、比赛和报名信息" in answer
    assert "匹配到" not in answer and "B302" not in answer and "A101" not in answer
    assert db.list_unanswered("g1") == []
    env.assert_not_awaited()


async def test_confirmed_team_definition_faq_still_takes_priority(env):
    db.add_faq("g1", "ACM集训队是什么", "老师维护的本群团队介绍")
    _competition()
    result = await commands.handle_non_command_message("ACM集训队是什么", "g1", "member")
    assert result[0]["text"].startswith("老师维护的本群团队介绍")
    env.assert_not_awaited()


@pytest.mark.parametrize("question", ["你在哪里", "ACM是什么", "图论训练是什么", "我到了", "ACM队好厉害"])
async def test_non_arrangement_text_does_not_recall_contests_or_courses(env, question):
    _competition()
    _course("图论训练")
    assert await faq_cmd._answer_from_knowledge_base(question, "g1") is None
    env.assert_not_awaited()


async def test_explicit_time_question_does_not_clarify_unasked_location(env):
    _competition(location="B302", time="2026-11-22 19:00")
    _competition(location="A101", time="2026-11-22 20:00", edition="第二场")
    _course()
    answer = await faq_cmd._answer_from_knowledge_base("ACM比赛什么时候开始", "g1")
    assert answer.startswith("时间：") and "请明确要查询哪一项" in answer
    assert "课程" not in answer and "地点" not in answer
    env.assert_not_awaited()


async def test_equal_requested_time_answers_without_unasked_location_ambiguity(env):
    _competition(location="B302")
    _competition(location="A101", edition="第二场")
    answer = await faq_cmd._answer_from_knowledge_base("ACM排位赛什么时候开始", "g1")
    assert "比赛时间：2026-11-22 19:00" in answer
    assert "请明确" not in answer and "地点" not in answer
    env.assert_not_awaited()


async def test_deadline_ambiguity_names_deadline_instead_of_location(env):
    _competition(deadline="2026-11-22 18:00")
    _competition(deadline="2026-11-22 17:00", edition="第二场")
    answer = await faq_cmd._answer_from_knowledge_base("ACM排位赛报名什么时候截止", "g1")
    assert answer.startswith("报名截止：") and "请明确" in answer
    assert "地点" not in answer
    env.assert_not_awaited()


async def test_location_ambiguity_names_location_and_only_requested_type(env):
    _competition(location="B302")
    _competition(location="A101", edition="第二场")
    _course()
    answer = await faq_cmd._answer_from_knowledge_base("ACM比赛地点在哪里", "g1")
    assert answer.startswith("地点：") and "请明确" in answer
    assert "课程" not in answer
    env.assert_not_awaited()


async def test_general_arrangement_ambiguity_is_generic_and_not_location_only(env):
    _competition()
    _competition(location="A101", edition="第二场")
    answer = await faq_cmd._answer_from_knowledge_base("ACM比赛安排", "g1")
    assert answer.startswith("匹配到多项安排") and "请明确" in answer
    assert not answer.startswith("地点：")
    env.assert_not_awaited()


async def test_exact_title_number_and_weekday_survive_query_boundary(env):
    _course("图论训练0", location="B302", weekday="周六")
    _course("图论训练01", location="A101", weekday="周六")
    _course("图论训练0", location="D401", weekday="周一")
    answer = await faq_cmd._answer_from_knowledge_base("周六图论训练0地点在哪里", "g1")
    assert "【图论训练0】" in answer and "地点：B302" in answer
    assert "D401" not in answer and "A101" not in answer
    env.assert_not_awaited()


async def test_which_classroom_is_an_arrangement_question(env):
    _course("图论训练", location="B302", weekday="周六")
    answer = await faq_cmd._answer_from_knowledge_base("图论训练教室是哪间", "g1")
    assert "地点：B302" in answer


def test_team_whereabouts_is_not_a_team_definition():
    assert faq_cmd._business_query_fields("ACM集训队所在地是什么") == ("location",)
    assert faq_cmd._is_arrangement_query("ACM集训队所在地是什么")
