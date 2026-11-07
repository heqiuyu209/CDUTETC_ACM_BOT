"""FAQ 主题/意图、群隔离、候选模型预算与缓存的离线行为回归。"""

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest

from cdutetc import commands, config, database, faq, llm


@pytest.fixture
def kb(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "faq.sqlite"))
    monkeypatch.setattr(config, "QA_FAQ_LLM_ENABLED", False)
    monkeypatch.setattr(config, "QA_MEMBER_KB_ONLY", True)
    monkeypatch.setattr(config, "LLM_API_KEY", "offline-test")
    monkeypatch.setattr(config, "QA_KB_MATCH_MIN_SCORE", 0.34)
    faq._choices.clear()
    database.init_db()
    chat = AsyncMock(side_effect=AssertionError("本地检索不应调模型"))
    monkeypatch.setattr(llm, "_chat_once", chat)
    return chat


async def test_same_topic_distinguishes_time_place_and_registration(kb):
    place = database.add_faq("g1", "周赛在哪里", "在 B201")
    when = database.add_faq("g1", "周赛什么时候开始", "周日 19:00")
    signup = database.add_faq("g1", "周赛怎么报名", "找负责老师登记")
    assert (await commands.match_faq("g1", "请问周赛在哪里？"))["id"] == place
    assert (await commands.match_faq("g1", "周赛啥时候开始呀"))["id"] == when
    assert (await commands.match_faq("g1", "周赛如何报名"))["id"] == signup
    kb.assert_not_awaited()


async def test_generic_keywords_and_multiple_questions_do_not_choose_wrong_faq(kb):
    database.add_faq("g1", "周赛怎么报名", "找老师")
    database.add_faq("g1", "校赛怎么报名", "填写校赛表格")
    database.add_faq("g1", "周赛什么时候", "周日")
    assert await commands.match_faq("g1", "怎么报名") is None
    assert await commands.match_faq("g1", "周赛怎么报名，什么时候截止") is None
    assert await commands.match_faq("g1", "今天食堂几点开饭") is None


async def test_equipment_synonym_and_exact_question_take_priority(kb):
    fid = database.add_faq("g1", "上课要带笔记本吗", "需要带电脑和充电器")
    assert (await commands.match_faq("g1", "带电脑吗"))["id"] == fid
    database.add_faq("g1", "上课要带笔记本吗", "最新安排：需要带电脑")
    answer = await commands.match_faq("g1", "上课要带笔记本吗")
    assert answer["answer"] == "最新安排：需要带电脑"


@pytest.mark.parametrize("question,stored", [
    ("校赛怎么报名", "周赛怎么报名"),
    ("蓝桥杯报名条件是什么", "校赛报名条件是什么"),
    ("周赛报名什么时候截止", "周赛什么时候"),
    ("周赛什么时候报名", "周赛什么时候截止报名"),
    ("周赛和校赛怎么报名", "校赛怎么报名"),
    ("周赛周六举办吗", "周赛周日举办吗"),
    ("周赛报名不用交钱吗", "周赛报名要交钱吗"),
    ("第2届校赛怎么报名", "第1届校赛怎么报名"),
    ("周赛2.5小时吗", "周赛25小时吗"),
    ("青鸟杯怎么报名", "星河杯怎么报名"),
    ("周赛什么时候", "周赛报名"),
])
async def test_different_objects_and_conditions_never_reuse_answer(kb, monkeypatch, question, stored):
    database.add_faq("g1", stored, "这不是所问对象或条件的答案")
    monkeypatch.setattr(config, "QA_FAQ_LLM_ENABLED", True)
    assert await commands.match_faq("g1", question) is None
    kb.assert_not_awaited()


async def test_relevant_candidate_wins_over_similar_wrong_event(kb):
    expected = database.add_faq("g1", "周赛怎么报名", "周赛登记表")
    database.add_faq("g1", "校赛怎么报名", "校赛登记表")
    assert (await commands.match_faq("g1", "请问周赛如何报名"))["id"] == expected


async def test_equivalent_weekday_wording_still_matches(kb):
    expected = database.add_faq("g1", "周赛周日举办吗", "本周日正常举办")
    assert (await commands.match_faq("g1", "周赛星期天举办吗"))["id"] == expected


async def test_member_reads_faq_once_and_records_unmatched_question(kb, monkeypatch):
    database.add_faq("g2", "本群课程地点", "另一群资料")
    read = Mock(wraps=database.list_faq)
    monkeypatch.setattr(database, "list_faq", read)
    replies = await commands.handle_non_command_message("课程在哪上", "g1", "member")
    read.assert_called_once_with("g1")
    assert "#" in replies[0]["text"]
    assert "另一群资料" not in replies[0]["text"]
    assert len(database.list_unanswered("g1")) == 1
    kb.assert_not_awaited()


def ambiguous_faqs(group="g1"):
    first = database.add_faq(group, "周赛什么时候", "周日 19:00")
    database.add_faq(group, "周赛在哪里", "B201")
    return first


async def test_optional_model_only_selects_id_and_reuses_same_group_cache(kb, monkeypatch):
    fid = ambiguous_faqs()
    monkeypatch.setattr(config, "QA_FAQ_LLM_ENABLED", True)
    kb.side_effect = None
    kb.return_value = json.dumps({"faq_id": fid})
    for _ in range(2):
        assert (await commands.match_faq("g1", "周赛安排"))["id"] == fid
    kb.assert_awaited_once()
    call = kb.await_args
    assert call.kwargs == {"json_mode": True, "max_tokens": 24}
    payload = json.loads(call.args[1])
    assert set(payload) == {"question", "candidates"}
    assert all(set(item) == {"id", "question"} for item in payload["candidates"])
    assert "B201" not in call.args[1] and "周日 19:00" not in call.args[1]


async def test_cache_is_invalidated_by_group_and_changed_answer(kb, monkeypatch):
    first = ambiguous_faqs()
    second = ambiguous_faqs("g2")
    monkeypatch.setattr(config, "QA_FAQ_LLM_ENABLED", True)
    kb.side_effect = [json.dumps({"faq_id": value}) for value in (first, second, first)]
    assert (await commands.match_faq("g1", "周赛安排"))["id"] == first
    assert (await commands.match_faq("g2", "周赛安排"))["id"] == second
    with database._connect() as conn:
        conn.execute("UPDATE group_faq SET answer='改期了' WHERE id=?", (first,))
    assert (await commands.match_faq("g1", "周赛安排"))["answer"] == "改期了"
    assert kb.await_count == 3


async def test_concurrent_identical_queries_share_one_model_call(kb, monkeypatch):
    fid = ambiguous_faqs()
    monkeypatch.setattr(config, "QA_FAQ_LLM_ENABLED", True)
    entered, release = asyncio.Event(), asyncio.Event()
    async def chat(*args, **kwargs):
        entered.set()
        await release.wait()
        return json.dumps({"faq_id": fid})
    kb.side_effect = chat
    workers = [asyncio.create_task(commands.match_faq("g1", "周赛安排")) for _ in range(3)]
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
    finally:
        release.set()
    assert all(item["id"] == fid for item in await asyncio.gather(*workers))
    kb.assert_awaited_once()


@pytest.mark.parametrize("selection", [999, True, "1", None, {"id": 1}])
async def test_invalid_model_id_cannot_return_arbitrary_answer(kb, monkeypatch, selection):
    ambiguous_faqs()
    monkeypatch.setattr(config, "QA_FAQ_LLM_ENABLED", True)
    kb.side_effect = None
    kb.return_value = json.dumps({"faq_id": selection})
    assert await commands.match_faq("g1", "周赛安排") is None


async def test_model_no_match_is_cached_but_local_exact_match_skips_model(kb, monkeypatch):
    fid = ambiguous_faqs()
    monkeypatch.setattr(config, "QA_FAQ_LLM_ENABLED", True)
    kb.side_effect = None
    kb.return_value = '{"faq_id":0}'
    assert await commands.match_faq("g1", "周赛安排") is None
    assert await commands.match_faq("g1", "周赛安排") is None
    kb.assert_awaited_once()
    assert (await commands.match_faq("g1", "周赛什么时候"))["id"] == fid
    assert kb.await_count == 1


async def test_model_timeout_and_failure_fall_back_to_unanswered(kb, monkeypatch):
    ambiguous_faqs()
    monkeypatch.setattr(config, "QA_FAQ_LLM_ENABLED", True)
    kb.side_effect = asyncio.TimeoutError
    replies = await commands.handle_non_command_message("周赛安排", "g1", "member")
    assert "#" in replies[0]["text"]
    assert len(database.list_unanswered("g1")) == 1


async def test_deleted_faq_cannot_be_returned_by_inflight_model(kb, monkeypatch):
    fid = ambiguous_faqs()
    monkeypatch.setattr(config, "QA_FAQ_LLM_ENABLED", True)
    async def chat(*args, **kwargs):
        database.delete_faq("g1", fid)
        return json.dumps({"faq_id": fid})
    kb.side_effect = chat
    assert await commands.match_faq("g1", "周赛安排") is None


async def test_expired_selection_cache_rechecks_model(kb, monkeypatch):
    fid = ambiguous_faqs()
    monkeypatch.setattr(config, "QA_FAQ_LLM_ENABLED", True)
    kb.side_effect = None
    kb.return_value = json.dumps({"faq_id": fid})
    tick = [100.0]
    monkeypatch.setattr(faq.time, "monotonic", lambda: tick[0])
    assert (await commands.match_faq("g1", "周赛安排"))["id"] == fid
    tick[0] += 301
    assert (await commands.match_faq("g1", "周赛安排"))["id"] == fid
    assert kb.await_count == 2
