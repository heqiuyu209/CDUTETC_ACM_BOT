"""具体字段查问只返回相关已登记事实，缺字段不猜；模型上下文和输出有界。"""

import json
from unittest.mock import AsyncMock

import pytest

from cdutetc import commands, config, database, llm


@pytest.fixture
def records(monkeypatch):
    monkeypatch.setattr(database, "list_question_contests", lambda group: [{
        "name": "ACM 周赛", "reg_deadline": "2026-11-08 12:00", "contest_time": "2026-11-08 19:00",
        "location": "B201", "mode": "", "source_link": "https://example.com",
    }])
    monkeypatch.setattr(database, "list_training_sessions", lambda *args, **kwargs: [])
    monkeypatch.setattr(database, "list_org_events", lambda *args, **kwargs: [])


async def test_location_question_does_not_dump_unasked_deadline(records):
    answer = await commands._answer_from_knowledge_base("周赛在哪", "g1")
    assert "B201" in answer
    assert "2026-11-08 12:00" not in answer
    assert "以群公告为准" not in answer


async def test_missing_requested_field_is_explicit_instead_of_invented(records):
    answer = await commands._answer_from_knowledge_base("周赛是线上还是线下", "g1")
    assert "还没登记" in answer
    assert "B201" not in answer


async def test_model_answer_keeps_factual_context_and_has_small_budget(monkeypatch):
    chat = AsyncMock(return_value="开赛时间是 2026-11-08 19:00。")
    monkeypatch.setattr(llm, "_chat_once", chat)
    monkeypatch.setattr(config, "QA_TOPIC_GATE_ENABLED", True)
    contests = [{"name": "周赛", "contest_time": "2026-11-08 19:00", "location": "B201"}]
    assert "2026-11-08 19:00" in await llm.answer_question("周赛什么时候", contests)
    call = chat.await_args
    assert call.kwargs["max_tokens"] == 240
    context = call.args[0].split("【存档数据】\n", 1)[1]
    assert json.loads(context) == contests
    assert "\n  " not in context
