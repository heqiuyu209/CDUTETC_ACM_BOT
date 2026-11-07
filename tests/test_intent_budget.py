"""发布意图识别不为明显提问、被拒绝输入付费，真实陈述保持受限识别。"""

from unittest.mock import AsyncMock

import pytest

from cdutetc import config, llm, main
from cdutetc.clients.onebot import OneBotMessage


@pytest.mark.parametrize("text", ["周赛什么时候", "周赛在哪报名", "你能自动提醒吗", "你好", "如何报名？"])
async def test_obvious_question_skips_publish_classifier_model(monkeypatch, text):
    chat = AsyncMock(side_effect=AssertionError("明显查询不需要付费分类"))
    monkeypatch.setattr(llm, "_chat_once", chat)
    assert await llm.classify_intent(text) == "other"
    chat.assert_not_awaited()


@pytest.mark.parametrize("text", ["本周日19:00进行ACM周赛，请大家提前准备", "新生选拔赛报名在机房登记", "本次邀请赛地点在B201"])
async def test_publication_statement_keeps_small_classification_budget(monkeypatch, text):
    chat = AsyncMock(return_value='{"intent":"add_contest"}')
    monkeypatch.setattr(llm, "_chat_once", chat)
    assert await llm.classify_intent(text) == "add_contest"
    assert chat.await_args.kwargs == {"json_mode": True, "max_tokens": 32}


async def test_blacklisted_staff_never_reaches_implicit_publish_classifier(monkeypatch, onebot_client):
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"teacher"})
    monkeypatch.setattr(config, "QA_BLACKLIST", {"teacher"})
    monkeypatch.setattr(config, "NL_INTENT_ENABLED", True)
    classifier = AsyncMock(return_value="add_contest")
    monkeypatch.setattr(llm, "classify_intent", classifier)
    bot = main.ACMBot(onebot_client)
    publish = AsyncMock()
    monkeypatch.setattr(bot, "_handle_publish", publish)
    await bot.on_group_at_message_create(OneBotMessage({
        "post_type": "message", "message_type": "group", "message_id": 1,
        "group_id": "g1", "user_id": "teacher", "self_id": 30001,
        "raw_message": "[CQ:at,qq=30001] 本周日19:00进行ACM周赛",
    }))
    classifier.assert_not_awaited()
    publish.assert_not_awaited()
