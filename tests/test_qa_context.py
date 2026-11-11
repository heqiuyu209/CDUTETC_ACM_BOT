"""动态群上下文（qa_context）与问答链路注入的行为验证。

覆盖：
- 课程窗口：未来 7 天（含今天）内、未取消、尚未结束的课程；更远的排除；
- 征集：仅 open 状态，带截止时间、剩余名额与登记字段；
- FAQ：本群问答对（已删除的不出现），单条过长按字符截断；
- 上下文为空 / 开关关闭时返回空串（不让模型多花 token）；
- 超长上下文按行截断并留标记；
- llm.answer_question 把事实块与命令白名单注入 system prompt；
- 老师路径默认注入上下文；普通成员默认零模型转工单，开启开关后模型判定
  未收录才转工单、命中事实则直接作答。
"""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from cdutetc import commands, config, database as db, llm, qa_context
from cdutetc.reminder import TZ

NOW = datetime(2026, 11, 11, 18, 0, tzinfo=TZ)  # 周三 18:00


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "qa_context.sqlite3"))
    monkeypatch.setattr(config, "QA_CONTEXT_ENABLED", True)
    monkeypatch.setattr(config, "QA_CONTEXT_MAX_CHARS", 3000)
    monkeypatch.setattr(config, "QA_MEMBER_KB_ONLY", True)
    monkeypatch.setattr(config, "QA_MEMBER_CONTEXT_ANSWER", False)
    monkeypatch.setattr(config, "QA_FAQ_LLM_ENABLED", False)
    monkeypatch.setattr(config, "QA_SOCIAL_CHAT_ENABLED", True)
    monkeypatch.setattr(config, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(qa_context, "_now", lambda value=None: value or NOW)
    db.init_db()
    return monkeypatch


def _course(group_id="g1", weekday="周三", start="19:00", end="21:00",
            title="图论进阶", location="实训楼 B302", **extra) -> int:
    return db.create_training_session(group_id, {
        "title": title, "weekday": weekday, "start_time": start,
        "end_time": end, "location": location, **extra,
    })


def _collection(group_id="g1", title="天梯赛选拔", deadline="2026-11-12 12:00",
                capacity=2, status="open") -> int:
    cid = db.create_collection(group_id, title, deadline=deadline, capacity=capacity,
                               fields=["姓名", "班级"])
    if status != "open":
        with db._connect() as conn:
            conn.execute("UPDATE collections SET status=? WHERE id=?", (status, cid))
    return cid


# --------------------------------------------------------------- 课程窗口

def test_week_sessions_window_and_ordering(env):
    today = _course(start="19:00", end="21:00", title="图论进阶")
    tomorrow = _course(weekday="周四", start="14:00", end="16:00", title="DP 专题")
    _course(weekday="周三", start="09:00", end="11:00", title="已结束的早课")   # 今天已结束
    _course(weekday="周四", start="19:00", end="21:00", title="已过生效期的课",
            effective_end="2026-11-01 00:00")                                # 生效期已过

    items = qa_context.week_sessions("g1", NOW)
    assert [item["id"] for item in items] == [today, tomorrow]
    assert items[0]["date_label"] == "11-11 周三"
    assert items[1]["date_label"] == "11-12 周四"


def test_week_sessions_covers_seven_days_only(env):
    _course(weekday="周二", start="19:00", end="21:00", title="下周二训练")  # 今天是周三，下周二在 6 天后
    titles = [item["title"] for item in qa_context.week_sessions("g1", NOW)]
    assert titles == ["下周二训练"]
    # 窗口收窄到 3 天时，下周二落在窗口外
    assert qa_context.week_sessions("g1", NOW, days=3) == []


def test_week_sessions_ignores_other_group(env):
    _course(group_id="g2")
    assert qa_context.week_sessions("g1", NOW) == []


# ----------------------------------------------------------------- 征集

def test_open_collections_only_open_with_capacity(env):
    opened = _collection(capacity=2)
    _collection(title="已关闭征集", status="closed")

    items = qa_context.open_collections("g1")
    assert [item["id"] for item in items] == [opened]
    assert items[0]["remaining"] == 2
    assert items[0]["fields"] == ["姓名", "班级"]


def test_collection_line_renders_details(env):
    _collection(capacity=2)
    context = qa_context.build_group_qa_context("g1", NOW)
    assert "[ID: 1] 天梯赛选拔" in context
    assert "截止 2026-11-12 12:00" in context
    assert "剩 2 个名额" in context
    assert "字段: 姓名、班级" in context


# ------------------------------------------------------------------ FAQ

def test_faq_pairs_skip_deleted_and_clip_long_answer(env):
    db.add_faq("g1", "机房钥匙找谁？", "找当天值日助教或负责人。")
    long_answer = "请假需提前私聊授课老师并说明事由，" * 20
    db.add_faq("g1", "请假怎么报备？", long_answer)
    db.add_faq("g1", "会被删掉的条目", "答案")
    with db._connect() as conn:
        conn.execute("UPDATE group_faq SET status='deleted' WHERE question='会被删掉的条目'")

    items = qa_context.faq_pairs("g1")
    assert [item["question"] for item in items] == ["请假怎么报备？", "机房钥匙找谁？"]
    context = qa_context.build_group_qa_context("g1", NOW)
    assert "Q: 机房钥匙找谁？ A: 找当天值日助教或负责人。" in context
    assert "会被删掉的条目" not in context
    assert "…" in context  # 超长答案被裁剪


# ------------------------------------------------------------- 组装与截断

def test_context_lists_all_three_sections(env):
    _course()
    _collection()
    db.add_faq("g1", "机房钥匙找谁？", "找当天值日助教或负责人。")

    context = qa_context.build_group_qa_context("g1", NOW)
    assert context.startswith(qa_context.CONTEXT_TITLE)
    assert "1. 近期课程" in context
    assert "2. 进行中的征集与报名" in context
    assert "3. 常见问题（FAQ）" in context
    assert "图论进阶（实训楼 B302）" in context


def test_empty_group_returns_empty_context(env):
    assert qa_context.build_group_qa_context("g1", NOW) == ""


def test_context_disabled_returns_empty(env):
    _course()
    env.setattr(config, "QA_CONTEXT_ENABLED", False)
    assert qa_context.build_group_qa_context("g1", NOW) == ""


def test_context_truncated_by_line_budget(env):
    for index in range(20):
        db.add_faq("g1", f"问题{index}号是什么？", f"答案{index}号")
    env.setattr(config, "QA_CONTEXT_MAX_CHARS", 200)

    context = qa_context.build_group_qa_context("g1", NOW)
    assert len(context) <= 220
    assert context.splitlines()[-1] == "…（上下文已截断，细节请用斜杠命令查询）"


# --------------------------------------------------- llm system prompt 注入

async def test_answer_question_injects_context_and_command_whitelist(env):
    env.setattr(config, "QA_TOPIC_GATE_ENABLED", False)
    captured = {}

    async def fake_chat(system, user, json_mode=False, **kwargs):
        captured["system"] = system
        return "本周三 19:00 在实训楼 B302 上图论进阶。"

    env.setattr(llm, "_chat_once", fake_chat)
    answer = await llm.answer_question(
        "这周有课吗", [], group_context="【当前集训队最新教务事实】\n1. 近期课程：\n- 图论进阶",
    )
    assert answer == "本周三 19:00 在实训楼 B302 上图论进阶。"
    assert qa_context.CONTEXT_TITLE in captured["system"]
    assert "/课程列表" in captured["system"]
    assert llm.UNKNOWN_MARKER in captured["system"]


async def test_answer_question_without_context_keeps_prompt_lean(env):
    env.setattr(config, "QA_TOPIC_GATE_ENABLED", False)
    captured = {}

    async def fake_chat(system, user, json_mode=False, **kwargs):
        captured["system"] = system
        return "ok"

    env.setattr(llm, "_chat_once", fake_chat)
    await llm.answer_question("校赛什么时候", [])
    assert qa_context.CONTEXT_TITLE not in captured["system"]
    assert llm.UNKNOWN_MARKER not in captured["system"]


# --------------------------------------------------------- 命令层链路接线

async def test_staff_question_passes_group_context(env):
    _course()
    _collection()
    db.add_faq("g1", "机房钥匙找谁？", "找当天值日助教或负责人。")
    answer = AsyncMock(return_value="周三 19:00 图论进阶。")
    env.setattr(llm, "answer_question", answer)

    replies = await commands.handle_non_command_message("课程时间是什么", "g1", "teacher", staff=True)

    assert replies[0]["text"] == "周三 19:00 图论进阶。"
    group_context = answer.await_args.kwargs["group_context"]
    assert "图论进阶" in group_context
    assert "[ID: 1] 天梯赛选拔" in group_context
    assert "机房钥匙找谁？" in group_context


async def test_staff_unknown_marker_becomes_ticket(env):
    answer = AsyncMock(return_value=llm.UNKNOWN_MARKER)
    env.setattr(llm, "answer_question", answer)

    replies = await commands.handle_non_command_message("队服在哪领", "g1", "teacher", staff=True)

    assert "已记录为问题 #1" in replies[0]["text"]
    assert [item["question"] for item in db.list_unanswered("g1")] == ["队服在哪领"]


async def test_member_default_keeps_zero_token_ticket(env):
    answer = AsyncMock(side_effect=AssertionError("普通成员默认不应调用模型"))
    env.setattr(llm, "answer_question", answer)

    replies = await commands.handle_non_command_message("队服在哪领", "g1", "member")

    answer.assert_not_awaited()
    assert "问题编号 #1" in replies[0]["text"]


async def test_member_context_answer_when_enabled(env):
    env.setattr(config, "QA_MEMBER_CONTEXT_ANSWER", True)
    _course()
    answer = AsyncMock(return_value="周三 19:00 到 21:00 有图论进阶。")
    env.setattr(llm, "answer_question", answer)

    replies = await commands.handle_non_command_message("明天有课吗", "g1", "member")

    assert replies[0]["text"] == "周三 19:00 到 21:00 有图论进阶。"
    assert db.list_unanswered("g1") == []
    assert "图论进阶" in answer.await_args.kwargs["group_context"]


async def test_member_unknown_falls_back_to_ticket(env):
    env.setattr(config, "QA_MEMBER_CONTEXT_ANSWER", True)
    env.setattr(llm, "answer_question", AsyncMock(return_value=llm.UNKNOWN_MARKER))

    replies = await commands.handle_non_command_message("队服在哪领", "g1", "member")

    assert "问题编号 #1" in replies[0]["text"]
    assert [item["question"] for item in db.list_unanswered("g1")] == ["队服在哪领"]


async def test_member_context_model_failure_falls_back_to_ticket(env):
    env.setattr(config, "QA_MEMBER_CONTEXT_ANSWER", True)
    env.setattr(llm, "answer_question", AsyncMock(side_effect=RuntimeError("boom")))

    replies = await commands.handle_non_command_message("队服在哪领", "g1", "member")

    assert "问题编号 #1" in replies[0]["text"]


async def test_staff_model_unavailable_message_distinct_from_unknown(env):
    env.setattr(llm, "answer_question", AsyncMock(side_effect=RuntimeError("boom")))

    replies = await commands.handle_non_command_message("队服在哪领", "g1", "teacher", staff=True)

    assert "模型暂时用不了" in replies[0]["text"]
    assert "没查到" not in replies[0]["text"]
    assert [item["question"] for item in db.list_unanswered("g1")] == ["队服在哪领"]


async def test_staff_real_llm_path_sees_facts_and_answers(env):
    """不 mock answer_question，只 mock 底层模型，验证事实块真的进了 prompt。"""
    env.setattr(config, "QA_TOPIC_GATE_ENABLED", False)
    _course()
    db.add_faq("g1", "机房钥匙找谁？", "找当天值日助教或负责人。")
    seen = {}

    async def fake_chat(system, user, json_mode=False, **kwargs):
        seen["system"] = system
        seen["user"] = user
        return "本周三 19:00-21:00 图论进阶，地点实训楼 B302。"

    env.setattr(llm, "_chat_once", fake_chat)
    replies = await commands.handle_non_command_message("这周课程怎么排", "g1", "teacher", staff=True)

    assert replies[0]["text"] == "本周三 19:00-21:00 图论进阶，地点实训楼 B302。"
    assert "图论进阶" in seen["system"]
    assert "机房钥匙找谁？" in seen["system"]
    assert "/课程列表" in seen["system"]


async def test_staff_real_llm_unknown_marker_becomes_ticket(env):
    env.setattr(config, "QA_TOPIC_GATE_ENABLED", False)
    env.setattr(llm, "_chat_once", AsyncMock(return_value=llm.UNKNOWN_MARKER))

    replies = await commands.handle_non_command_message("队服在哪领", "g1", "teacher", staff=True)

    assert "已记录为问题 #1" in replies[0]["text"]
    assert [item["question"] for item in db.list_unanswered("g1")] == ["队服在哪领"]
