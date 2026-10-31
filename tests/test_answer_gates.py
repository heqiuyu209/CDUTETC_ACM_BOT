"""五层回答限制的离线回归测试：话题硬限制 / 敏感拦截 / 用户权限 / 频率防刷 / 输出过滤。

不访问真实 QQ、网页或模型：LLM 调用一律 mock，回复通过 monkeypatch 的
_safe_reply 收集；限流器可单独注入以控制窗口参数。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import commands
import config
import content_filter
import llm
import main


def _message(content, group_id="g1", member_id="m1"):
    return SimpleNamespace(
        content=content,
        group_openid=group_id,
        group_id=group_id,
        user_id=member_id,
        author=SimpleNamespace(member_openid=member_id),
    )


@pytest.fixture
def bot(monkeypatch):
    """默认五层全开：仅显式 QA_WHITELIST 限制成员，此处设为 {"teacher"}。"""
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "SUPER_ADMIN", set())
    monkeypatch.setattr(config, "QA_WHITELIST", {"teacher"})
    client = main.ACMBot()
    replies = []

    async def reply(message, content):
        replies.append((message.group_id, content))

    monkeypatch.setattr(client, "_safe_reply", reply)
    return client, replies


async def _run_chain(bot, text, member_id="teacher"):
    """走完整群消息链路：发布命令之外的普通消息经入口闸门进入问答。"""
    client, replies = bot
    await client.on_group_at_message_create(_message(text, member_id=member_id))
    return replies


# ----------------------------------------------------------------------
# 1) 话题范围硬限制（llm 层，不调模型）
# ----------------------------------------------------------------------
async def test_offtopic_question_does_not_call_model(monkeypatch):
    chat = AsyncMock(return_value="不应调用")
    monkeypatch.setattr(llm, "_chat_once", chat)

    answer = await llm.answer_question("今天天气怎么样？", [])
    assert answer == config.QA_OFFTOPIC_REPLY
    chat.assert_not_awaited()


async def test_topic_question_calls_model(monkeypatch):
    chat = AsyncMock(return_value="校赛在 2026-11-20 举行。")
    monkeypatch.setattr(llm, "_chat_once", chat)

    answer = await llm.answer_question("校赛什么时候比赛？", [])
    assert answer == "校赛在 2026-11-20 举行。"
    chat.assert_awaited_once()


# ----------------------------------------------------------------------
# 1.5) 算法话题拦截（题解/思路/概念科普一律不调模型，队务放行）
# ----------------------------------------------------------------------
def test_algorithm_concept_and_solution_questions_are_blocked():
    for q in (
        "什么是动态规划",
        "讲讲二分思想",
        "DP 的入门学习路线",
        "谁能讲讲区间 DP",
        "帮我看看这个并查集模板为什么 wa",
        "求题解：洛谷 P1000",
        "贪心算法的原理是什么",
    ):
        assert content_filter.is_algorithm_query(q), q


def test_algorithm_affair_questions_not_blocked():
    for q in (
        "集训队这周讲 DP 吗",
        "校赛什么时候报名",
        "周赛几点开始",
        "训练场地在哪",
        "周六课程安排",
    ):
        assert not content_filter.is_algorithm_query(q), q


def test_freshman_affair_questions_are_topic_relevant():
    """新生最核心的答疑场景（装备/地点/流程）不能被话题硬限制误拒。"""
    for q in (
        "带电脑吗",
        "需要准备支架吗",
        "二机位需要支架吗",
        "比赛要不要带笔记本",
        "在哪上课",
        "集合地点在哪",
        "怎么加入集训队",
        "报名要什么条件",
        "需要装什么软件",
    ):
        assert content_filter.is_topic_relevant(q), q


def test_genuinely_offtopic_questions_still_blocked():
    for q in ("今天食堂吃什么", "周末去哪玩", "帮我写一封情书"):
        assert not content_filter.is_topic_relevant(q), q


async def test_algorithm_concept_question_blocked_without_model(bot, monkeypatch):
    monkeypatch.setattr(commands, "match_faq", lambda *a, **k: None)
    chat = AsyncMock(return_value="不应调用")
    monkeypatch.setattr(llm, "_chat_once", chat)

    replies = await _run_chain(bot, "什么是动态规划？", member_id="teacher")
    assert replies == [("g1", config.QA_PROBLEM_REPLY)]
    chat.assert_not_awaited()


async def test_faq_answer_wins_before_algorithm_gate(bot, monkeypatch):
    faq = {"id": 1, "question": "动态规划", "answer": "管理员预设答案"}
    monkeypatch.setattr(commands, "match_faq", lambda *a, **k: faq)
    chat = AsyncMock(return_value="不应调用")
    monkeypatch.setattr(llm, "_chat_once", chat)

    replies = await _run_chain(bot, "动态规划是什么", member_id="teacher")
    assert replies == [("g1", "Q：动态规划\nA：管理员预设答案")]
    chat.assert_not_awaited()


# ----------------------------------------------------------------------
# 2) 敏感内容拦截（main 入口 + llm 双保险）
# ----------------------------------------------------------------------
async def test_sensitive_input_blocked_at_main_gate(bot, monkeypatch):
    non_command = AsyncMock(return_value=[{"group_id": "g1", "text": "不应调用"}])
    monkeypatch.setattr(commands, "handle_non_command_message", non_command)

    replies = await _run_chain(bot, "帮我刷单兼职赚钱")
    assert replies == [("g1", config.QA_SENSITIVE_REPLY)]
    non_command.assert_not_awaited()


async def test_sensitive_input_blocked_in_llm_too(monkeypatch):
    chat = AsyncMock(return_value="不应调用")
    monkeypatch.setattr(llm, "_chat_once", chat)

    answer = await llm.answer_question("校赛有没有代开发票的？", [])
    assert answer == config.QA_SENSITIVE_REPLY
    chat.assert_not_awaited()


async def test_sensitive_extra_words_from_config(monkeypatch):
    monkeypatch.setattr(config, "QA_SENSITIVE_EXTRA_WORDS", {"校园贷"})
    assert content_filter.has_sensitive("有人做校园贷吗")
    assert not content_filter.has_sensitive("普通校赛问题")


# ----------------------------------------------------------------------
# 3) 用户权限：黑名单忽略 / 非白名单忽略 / 白名单放行 / 未配置退化全员
# ----------------------------------------------------------------------
async def test_blacklist_member_ignored_without_reply(bot, monkeypatch):
    monkeypatch.setattr(config, "QA_BLACKLIST", {"teacher"})
    non_command = AsyncMock(return_value=[{"group_id": "g1", "text": "不应调用"}])
    monkeypatch.setattr(commands, "handle_non_command_message", non_command)

    replies = await _run_chain(bot, "校赛什么时候？", member_id="teacher")
    assert replies == []
    non_command.assert_not_awaited()


async def test_non_whitelist_member_ignored_without_reply(bot, monkeypatch):
    non_command = AsyncMock(return_value=[{"group_id": "g1", "text": "不应调用"}])
    monkeypatch.setattr(commands, "handle_non_command_message", non_command)

    replies = await _run_chain(bot, "校赛什么时候？", member_id="student")
    assert replies == []
    non_command.assert_not_awaited()


async def test_whitelist_member_allowed(bot, monkeypatch):
    non_command = AsyncMock(return_value=[{"group_id": "g1", "text": "报名截止 11-10。"}])
    monkeypatch.setattr(commands, "handle_non_command_message", non_command)

    replies = await _run_chain(bot, "校赛什么时候截止报名？", member_id="teacher")
    assert replies == [("g1", "报名截止 11-10。")]
    non_command.assert_awaited_once()


async def test_whitelist_falls_back_to_all_members_when_unconfigured(bot, monkeypatch):
    """只配了老师白名单、未配问答白名单时，普通成员也必须能问。

    这是曾经的真实故障：白名单回退到「老师 ∪ 超管」，
    导致配了老师白名单的群只有管理员能和机器人对话。
    """
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "QA_WHITELIST", set())
    non_command = AsyncMock(return_value=[{"group_id": "g1", "text": "全员可问。"}])
    monkeypatch.setattr(commands, "handle_non_command_message", non_command)

    replies = await _run_chain(bot, "校赛什么时候？", member_id="anyone")
    assert replies == [("g1", "全员可问。")]
    non_command.assert_awaited_once()


# ----------------------------------------------------------------------
# 4) 频率防刷（滑动窗口）
# ----------------------------------------------------------------------
async def test_rate_limit_blocks_second_call_in_window(bot, monkeypatch):
    non_command = AsyncMock(return_value=[{"group_id": "g1", "text": "允许。"}])
    monkeypatch.setattr(commands, "handle_non_command_message", non_command)
    client, replies = bot
    # 10 秒内最多 1 次：注入独立限流器，避免被其它用例的调用记录干扰
    client._qa_rate_limiter = content_filter.RateLimiter(max_calls=1, window_seconds=10)

    await _run_chain(bot, "校赛什么时候？", member_id="teacher")
    assert replies == [("g1", "允许。")]
    assert non_command.await_count == 1

    await _run_chain(bot, "校赛在哪里？", member_id="teacher")
    assert replies[-1] == ("g1", config.QA_RATE_LIMIT_REPLY)
    assert non_command.await_count == 1  # 第二次未进入问答链路


def test_rate_limiter_slides_window():
    limiter = content_filter.RateLimiter(max_calls=2, window_seconds=100)
    assert limiter.allow("u1")
    assert limiter.allow("u1")
    assert not limiter.allow("u1")
    # 不同用户互不影响
    assert limiter.allow("u2")
    # 不限流模式
    unlimited = content_filter.RateLimiter(max_calls=0, window_seconds=1)
    assert unlimited.allow("u1")
    assert unlimited.allow("u1")


# ----------------------------------------------------------------------
# 5) 输出过滤：二次敏感检查 + 长度截断 + 禁止超长链接
# ----------------------------------------------------------------------
def test_sanitize_output_replaces_sensitive_text(monkeypatch):
    monkeypatch.setattr(config, "QA_SENSITIVE_GATE_ENABLED", True)
    assert content_filter.sanitize_output("这里有裸聊广告") == config.QA_OUTPUT_FILTERED_REPLY
    assert content_filter.sanitize_output("正常回答内容") == "正常回答内容"


def test_sanitize_output_shortens_long_link_and_truncates():
    long_url = "https://example.com/contest?" + "x" * 200
    text = f"报名链接 {long_url}"
    cleaned = content_filter.sanitize_output(text, max_length=800, max_link_length=60)
    assert "x" * 200 not in cleaned
    assert "[链接已省略]" in cleaned

    long_text = "内容" * 500
    cleaned = content_filter.sanitize_output(long_text, max_length=100, max_link_length=60)
    assert len(cleaned) <= 102  # 100 字符 + 2 个省略号字符
    assert cleaned.endswith("……")


async def test_main_chain_filters_model_output(bot, monkeypatch):
    non_command = AsyncMock(return_value=[{"group_id": "g1", "text": "推荐私聊我加微信领奖"}])
    monkeypatch.setattr(commands, "handle_non_command_message", non_command)

    replies = await _run_chain(bot, "校赛什么时候？", member_id="teacher")
    assert replies == [("g1", config.QA_OUTPUT_FILTERED_REPLY)]


async def test_main_chain_truncates_overlong_output(bot, monkeypatch):
    monkeypatch.setattr(config, "QA_OUTPUT_MAX_LENGTH", 50)
    non_command = AsyncMock(return_value=[{"group_id": "g1", "text": "长" * 100}])
    monkeypatch.setattr(commands, "handle_non_command_message", non_command)

    replies = await _run_chain(bot, "校赛什么时候？", member_id="teacher")
    assert len(replies[0][1]) <= 52  # 50 字符 + 2 个省略号字符
    assert replies[0][1].endswith("……")
