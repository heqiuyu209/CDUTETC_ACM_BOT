"""有效本群事实、零 Token 课程问答和拟人对话预算的真实 SQLite 回归。"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from cdutetc import commands, config, database as db, llm, main, persona_chat, qa_context
from cdutetc.handlers import faq_cmd

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 19, 14, tzinfo=TZ)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "trusted_context.sqlite3"))
    monkeypatch.setattr(db, "_delivery_now", lambda value=None: value or NOW)
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "QA_CONTEXT_ENABLED", True)
    monkeypatch.setattr(config, "QA_FAQ_LLM_ENABLED", False)
    monkeypatch.setattr(config, "QA_SOCIAL_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "PERSONA_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "QA_RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(config, "QA_WHITELIST_ENABLED", False)
    monkeypatch.setattr(config, "QA_BLACKLIST", set())
    monkeypatch.setattr(config, "MEME_ENABLED", False)
    monkeypatch.setattr(qa_context, "_now", lambda value=None: value or NOW)
    persona_path = tmp_path / "persona.txt"
    persona_path.write_text("你是可靠且俏皮的学姐。\n状态：{status}", encoding="utf-8")
    monkeypatch.setattr(persona_chat, "PERSONA_FILE", persona_path)
    persona_chat.reset_engine()
    db.init_db()
    return SimpleNamespace(monkeypatch=monkeypatch, persona_path=persona_path)


def course(group="g1", title="图论基础", start="19:00", end="21:00", **extra):
    return db.create_training_session(group, {"title": title, "weekday": "周四", "start_time": start,
                                             "end_time": end, "location": "B302", **extra}, "admin")


def tokens(system, user):
    return persona_chat.estimate_tokens(system) + persona_chat.estimate_tokens(user)


def test_week_context_excludes_expired_and_not_yet_effective_courses(env):
    course(title="仅上周一次", effective_start="2026-11-12 00:00", effective_end="2026-11-12 23:59")
    course(title="下周才生效", effective_start="2026-11-26 00:00")
    course(title="已取消", status="cancelled")
    assert qa_context.weekly_courses("g1", NOW) == []
    assert qa_context.build_group_qa_context("g1", NOW) == ""


def test_single_day_course_keeps_actual_date_and_is_not_weekly_rule(env):
    course(title="今日单次集训", effective_start="2026-11-19 00:00", effective_end="2026-11-19 23:59")
    items = qa_context.weekly_courses("g1", NOW)
    assert items[0]["date_text"] == "2026-11-19" and items[0]["recurring"] is False
    context = qa_context.build_group_qa_context("g1", NOW)
    assert "单次安排" in context
    weekly_section = context.split("4. 每周常规训练", 1)[1]
    assert "今日单次集训" not in weekly_section
    assert "每周四" not in weekly_section
    assert qa_context.weekly_courses("g1", NOW + timedelta(days=7)) == []


def test_one_occurrence_spanning_midnight_is_not_weekly_rule(env):
    course(title="单次夜间训练", start="23:00", end="01:00", effective_start="2026-11-19 00:00",
           effective_end="2026-11-20 02:00")
    context = qa_context.build_group_qa_context("g1", NOW)
    assert "单次夜间训练" in context and "单次安排" in context
    assert "单次夜间训练" not in context.split("4. 每周常规训练", 1)[1]


def test_real_repeating_course_keeps_weekly_rule_and_normalized_clocks(env):
    course(start="19-21点", end="")
    context = qa_context.build_group_qa_context("g1", NOW)
    assert "周四 19:00-21:00" in context
    assert "每周四 19:00-21:00" in context
    assert len(qa_context.week_sessions("g1", NOW)) == 1


@pytest.mark.parametrize("staff", [False, True])
async def test_member_and_teacher_course_time_answer_uses_local_fact_no_model(env, staff):
    course()
    model = AsyncMock(side_effect=AssertionError("已登记事实不应调用模型"))
    env.monkeypatch.setattr(llm, "answer_question", model)
    result = await commands.handle_non_command_message("图论基础什么时候上课", "g1", "teacher" if staff else "member",
                                                       staff=staff)
    assert "周四 19:00-21:00" in result[0]["text"]
    assert "B302" in result[0]["text"]
    model.assert_not_awaited()


async def test_missing_course_time_is_explicit_and_not_guessed(env):
    course(start="", end="")
    answer = await faq_cmd._answer_from_knowledge_base("图论基础几点上课", "g1")
    assert "时间：还没登记" in answer and "19:00" not in answer


async def test_local_single_course_date_uses_occurrence_not_effective_start_date(env):
    course(effective_start="2026-11-16 00:00", effective_end="2026-11-20 23:59")
    answer = await faq_cmd._answer_from_knowledge_base("图论基础什么时候上课", "g1")
    assert "2026-11-19 周四 19:00-21:00" in answer
    assert "2026-11-16 周四" not in answer


async def test_model_qa_context_receives_question_and_only_related_faqs(env):
    for step in ("报备", "填写", "提交", "申请", "处理"):
        db.add_faq("g1", f"请假流程怎么{step}", f"请假{step}请联系负责老师")
    db.add_faq("g1", "机房钥匙找谁", "绝不应出现的无关钥匙答案")
    captured = AsyncMock(return_value="请假联系老师。")
    env.monkeypatch.setattr(llm, "answer_question", captured)
    await faq_cmd._answer_question_with_context("请假流程怎么做", "g1")
    context = captured.await_args.kwargs["group_context"]
    assert "请假" in context
    assert context.count("- #") <= 3
    assert "无关钥匙答案" not in context


def test_compact_facts_are_relevant_scoped_bounded_and_skip_pure_banter(env):
    course()
    course("g2", title="私密二群课程", location="SECRET")
    for index in range(10):
        course(title=f"图论训练{index}")
    db.add_faq("g2", "图论基础在哪", "二群私密FAQ")
    facts = qa_context.compact_business_facts("g1", "图论基础的集训地点", NOW)
    assert "图论基础" in facts and "2026-11-19" in facts and "B302" in facts
    assert "SECRET" not in facts and "二群" not in facts
    assert len(facts.splitlines()) <= 3
    assert persona_chat.estimate_tokens(facts) <= 160
    assert qa_context.compact_business_facts("unknown", "课程安排", NOW) == ""
    assert qa_context.compact_business_facts("g1", "在吗", NOW) == ""
    assert qa_context.compact_business_facts("g1", "红温了，谁懂啊", NOW) == ""
    assert qa_context.compact_business_facts("g1", "刚打完训练累死了", NOW) == ""


def test_compact_course_facts_respect_relative_day_and_explicit_weekday(env):
    course(title="周四图论")
    course(title="周五动态规划", weekday="周五")
    facts = qa_context.compact_business_facts("g1", "明天的课程安排", NOW)
    assert "周五动态规划" in facts and "周四图论" not in facts
    facts = qa_context.compact_business_facts("g1", "这周四的课程安排", NOW)
    assert "周四图论" in facts and "周五动态规划" not in facts


def test_compact_contests_exclude_drafts_scope_and_archive_and_keep_unknown_time(env):
    cid = db.create_competition({"name": "正式校赛", "official_time": ""}, status="confirmed")
    other = db.create_competition({"name": "二群校赛", "official_time": "2026-11-21 19:00"}, status="confirmed")
    db.set_competition_notify_groups(other, {"g2"})
    db.create_competition({"name": "草稿校赛", "official_time": "2026-11-21 19:00"}, status="draft")
    facts = qa_context.compact_business_facts("g1", "正式校赛在哪", NOW)
    assert "正式校赛" in facts and "时间待定" in facts and "地点待定" in facts
    assert "二群校赛" not in facts and "草稿校赛" not in facts
    with db._connect() as conn:
        conn.execute("INSERT INTO group_competition_archives(group_id,competition_id,reason,archived_at,archived_by)"
                     " VALUES ('g1',?,'test','','admin')", (cid,))
    assert "正式校赛" not in qa_context.compact_business_facts("g1", "正式校赛在哪", NOW)


def test_compact_contest_deadline_is_original_field_not_start_time(env):
    db.create_competition({"name": "正式校赛", "official_time": "2026-11-21 19:00",
                           "official_reg_deadline": "2026-11-20 23:00"}, status="confirmed")
    facts = qa_context.compact_business_facts("g1", "正式校赛什么时候截止报名", NOW)
    assert "报名截止 2026-11-20 23:00" in facts
    assert "2026-11-21 19:00" in facts


def test_compact_tomorrow_contest_excludes_today_and_already_started_same_title(env):
    db.create_competition({"name": "校赛", "edition": "上午", "official_time": "2026-11-19 09:00"}, status="confirmed")
    db.create_competition({"name": "校赛", "edition": "今天", "official_time": "2026-11-19 19:00"}, status="confirmed")
    db.create_competition({"name": "校赛", "edition": "明天", "official_time": "2026-11-20 19:00"}, status="confirmed")
    facts = qa_context.compact_business_facts("g1", "明天校赛什么时候开始", NOW)
    assert "2026-11-20 19:00" in facts
    assert "2026-11-19" not in facts
    today = qa_context.compact_business_facts("g1", "今天校赛什么时候开始", NOW)
    assert "2026-11-19 19:00" in today and "09:00" not in today


def test_compact_contest_explicit_weekday_excludes_other_day_same_title(env):
    db.create_competition({"name": "校赛", "edition": "周四", "official_time": "2026-11-19 19:00"}, status="confirmed")
    db.create_competition({"name": "校赛", "edition": "周五", "official_time": "2026-11-20 19:00"}, status="confirmed")
    facts = qa_context.compact_business_facts("g1", "这周五校赛在哪", NOW)
    assert "2026-11-20 19:00" in facts
    assert "2026-11-19" not in facts


def test_compact_collections_only_publish_counts_not_member_details(env):
    cid = db.create_collection("g1", "集训报名", deadline="2026-11-20 19:00", capacity=3, fields=["姓名", "学号"])
    db.register_collection(cid, "g1", "member", {"姓名": "秘密姓名", "学号": "SECRET"})
    facts = qa_context.compact_business_facts("g1", "集训报名还有名额吗", NOW)
    assert "剩 2 名额" in facts
    assert "秘密姓名" not in facts and "SECRET" not in facts


def test_persona_clock_and_business_facts_survive_history_and_personal_impression_trimming(env):
    business = "课程 图论基础｜2026-11-19 周四 19:00-21:00｜B302"
    env.persona_path.write_text("长人设。" * 500 + "\n状态：{status}", encoding="utf-8")
    system, user = persona_chat.build_conversation(
        nickname="小明", text="图论基础还是在B302吗", status_text="在宿舍",
        facts=["我喜欢B999" * 20] * 10, context=["聊天中乱说周五七点" * 20] * 30,
        business_facts=business, now=NOW,
    )
    assert business in user
    assert "当前北京时间：2026-11-19 14:00（周四）" in user
    assert "个人印象不能作证" in user
    assert tokens(system, user) <= 700


def test_persona_caps_fact_rows_tokens_and_small_custom_budget(env):
    facts = "\n".join(f"正式课程{index}｜周四19:00｜B302" for index in range(20))
    for budget in (700, 300, 100, 10):
        system, user = persona_chat.build_conversation(nickname="小明", text="课程地点", status_text="在宿舍",
                                                       business_facts=facts, now=NOW, budget=budget)
        assert tokens(system, user) <= budget
        assert "正式课程3" not in user


async def test_persona_flow_loads_facts_as_separate_block_and_keeps_current_time(env):
    course()
    bot = main.ACMBot(SimpleNamespace())
    env.monkeypatch.setattr(bot, "_safe_reply", AsyncMock(return_value=True))
    chat = AsyncMock(return_value="在 B302，别走错啦。")
    env.monkeypatch.setattr(persona_chat, "chat", chat)
    message = SimpleNamespace(raw={}, id="inbound")
    assert await bot._handle_persona_chat(message, "图论基础集训地点还是B302", "g1", "member")
    system, user = chat.await_args.args
    assert "本群已登记事实" in user and "图论基础" in user and "19:00-21:00" in user
    assert "当前北京时间" in user
    assert tokens(system, user) <= 700
    chat.reset_mock()
    assert await bot._handle_persona_chat(message, "在吗", "g1", "member")
    assert "本群已登记事实" not in chat.await_args.args[1]


async def test_competition_group_location_overlay_reaches_local_answer_and_persona(env):
    from cdutetc import business_followup

    cid = db.create_competition({"name": "正式校赛", "official_time": "2026-11-21 19:00"}, status="confirmed")
    assert business_followup.set_competition_location("g1", "teacher", cid, "B302", now=NOW)
    local = await commands.handle_non_command_message("正式校赛在哪", "g1", "teacher", staff=True)
    assert "地点：B302" in local[0]["text"]
    assert "B302" in qa_context.compact_business_facts("g1", "正式校赛地点", NOW)
    other = qa_context.compact_business_facts("g2", "正式校赛地点", NOW)
    assert "B302" not in other and "地点待定" in other


@pytest.mark.parametrize("kind", ["course", "competition"])
@pytest.mark.parametrize("index", range(15))
async def test_local_numeric_titles_select_exact_record_among_fifteen(env, kind, index):
    from cdutetc import business_followup

    prefix = "图论训练" if kind == "course" else "校内选拔赛"
    for number in range(15):
        if kind == "course":
            course(title=f"{prefix}{number}", location=f"B{number:03d}")
        else:
            cid = db.create_competition({"name": f"{prefix}{number}",
                                         "official_time": "2026-11-21 19:00"}, status="confirmed")
            assert business_followup.set_competition_location("g1", "admin", cid, f"B{number:03d}", NOW)
    answer = await faq_cmd._answer_from_knowledge_base(f"{prefix}{index}地点在哪里", "g1")
    assert f"地点：B{index:03d}" in answer
    assert (f"【{prefix}{index}】" if kind == "course" else f"「{prefix}{index}」") in answer


@pytest.mark.parametrize("kind", ["course", "competition"])
async def test_numeric_title_boundary_distinguishes_zero_from_zero_one(env, kind):
    from cdutetc import business_followup

    prefix = "图论训练" if kind == "course" else "校内选拔赛"
    for suffix, location in (("0", "B302"), ("01", "A101")):
        if kind == "course":
            course(title=prefix + suffix, location=location)
        else:
            cid = db.create_competition({"name": prefix + suffix,
                                         "official_time": "2026-11-21 19:00"}, status="confirmed")
            assert business_followup.set_competition_location("g1", "admin", cid, location, NOW)
    first = await faq_cmd._answer_from_knowledge_base(prefix + "0地点在哪里", "g1")
    second = await faq_cmd._answer_from_knowledge_base(prefix + "01地点在哪里", "g1")
    assert "地点：B302" in first and "A101" not in first
    assert "地点：A101" in second and "B302" not in second


async def test_full_course_title_beats_fuzzy_competition_before_course_lookup(env):
    from cdutetc import business_followup

    course(title="图论训练", location="B302")
    cid = db.create_competition({"name": "图论训练赛", "official_time": "2026-11-21 19:00"}, status="confirmed")
    assert business_followup.set_competition_location("g1", "admin", cid, "A101", NOW)
    answer = await faq_cmd._answer_from_knowledge_base("图论训练地点在哪里", "g1")
    assert "【图论训练】" in answer and "地点：B302" in answer
    assert "A101" not in answer


@pytest.mark.parametrize("staff", [False, True])
async def test_same_title_different_locations_asks_object_without_model(env, staff):
    course(title="图论训练", location="B302")
    course(title="图论训练", location="A101")
    model = AsyncMock(side_effect=AssertionError("歧义不能交给模型猜测"))
    env.monkeypatch.setattr(llm, "answer_question", model)
    result = await commands.handle_non_command_message(
        "图论训练地点在哪里", "g1", "teacher" if staff else "member", staff=staff)
    answer = result[0]["text"]
    assert "请明确要查询哪一项" in answer
    assert "课程 1：图论训练" in answer and "课程 2：图论训练" in answer
    assert "地点：B302" not in answer and "地点：A101" not in answer
    model.assert_not_awaited()


async def test_same_title_explicit_weekday_selects_correct_course(env):
    course(title="图论训练", location="B302", weekday="周四")
    course(title="图论训练", location="A101", weekday="周一")
    answer = await faq_cmd._answer_from_knowledge_base("周四图论训练地点在哪里", "g1")
    assert "周四 19:00-21:00" in answer and "地点：B302" in answer
    assert "A101" not in answer


async def test_fuzzy_equal_score_different_locations_asks_object(env):
    course(title="ACM图论基础", location="B302")
    course(title="ACM动态规划", location="A101")
    answer = await faq_cmd._answer_from_knowledge_base("ACM课程地点在哪里", "g1")
    assert "请明确要查询哪一项" in answer
    assert "ACM图论基础" in answer and "ACM动态规划" in answer
    assert "地点：B302" not in answer and "地点：A101" not in answer
