"""五层回答限制的离线回归测试：话题硬限制 / 敏感拦截 / 用户权限 / 频率防刷 / 输出过滤。

不访问真实 QQ、网页或模型：LLM 调用一律 mock，回复通过 monkeypatch 的
_safe_reply 收集；限流器可单独注入以控制窗口参数。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import sqlite3

import pytest

import commands
import config
import content_filter
import database
import llm
import main


@pytest.fixture
def memory_db(monkeypatch):
    """把 database._connect 指向同一个内存库，测试结束即销毁。"""
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    database.init_db()
    yield conn
    conn.close()


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


# ----------------------------------------------------------------------
# 7) 本地知识库检索：口语问法要能召回，无关问题不能误命中
# ----------------------------------------------------------------------
def test_colloquial_question_matches_weekly_contest():
    """「这周有周赛吗」与记录名 Dice 仅 0.18，必须靠关键词命中召回。"""
    score = content_filter.record_match_score("这周有周赛吗", "ACM 周赛（本周）", "")
    assert score >= config.QA_KB_MATCH_MIN_SCORE


def test_unrelated_question_does_not_match_contest():
    for question in ("食堂几点开饭", "今天天气怎么样"):
        score = content_filter.record_match_score(question, "ACM 周赛（本周）", "")
        assert score < config.QA_KB_MATCH_MIN_SCORE


def test_common_word_does_not_cause_false_match():
    """「大学」这类泛词不应让「大学什么时候开学」命中学校比赛名。"""
    score = content_filter.record_match_score(
        "大学什么时候开学", "成都理工大学ACM排名赛", ""
    )
    assert score < config.QA_KB_MATCH_MIN_SCORE


async def test_member_weekly_question_answered_without_model(monkeypatch):
    """普通成员问周赛：命中已录入记录，且不产生模型调用。"""
    contest = {
        "name": "ACM 周赛（本周）",
        "reg_deadline": "",
        "contest_time": "2026-11-01 19:00",
        "mode": "",
        "location": "",
        "source_link": "",
    }
    monkeypatch.setattr(commands.db, "list_question_contests", lambda gid: [contest])
    monkeypatch.setattr(commands.db, "list_training_sessions", lambda *a, **k: [])
    monkeypatch.setattr(commands.db, "list_org_events", lambda *a, **k: [])
    chat = AsyncMock(return_value="不应调用")
    monkeypatch.setattr(llm, "_chat_once", chat)
    monkeypatch.setattr(config, "QA_MEMBER_KB_ONLY", True)

    replies = await commands.handle_non_command_message(
        "这周有周赛吗", "g1", "student", staff=False
    )
    assert "ACM 周赛（本周）" in replies[0]["text"]
    assert "2026-11-01 19:00" in replies[0]["text"]
    chat.assert_not_awaited()


# ----------------------------------------------------------------------
# 8) 口语问法归一：同一意图共用编号，回答后自动命中 FAQ
# ----------------------------------------------------------------------
def test_question_core_strips_particles():
    for question in ("这周有周赛吗", "这周有周赛没", "周赛有没"):
        assert content_filter.question_core(question) == "周赛"


def test_same_intent_true_for_synonyms():
    assert content_filter.same_intent("这周有周赛吗", "周赛有没")


def test_same_intent_false_for_different_topics():
    assert not content_filter.same_intent("这周有周赛吗", "这周有课吗")
    assert not content_filter.same_intent("这周有周赛吗", "食堂几点开饭")


def test_synonym_questions_share_one_id(memory_db):
    """三种口语问法必须复用同一个编号，不能每次新增。"""
    ids = {
        database.add_unanswered_question("g1", q, "s1")
        for q in ("这周有周赛吗", "这周有周赛没", "周赛有没")
    }
    assert len(ids) == 1
    assert len(database.list_unanswered("g1")) == 1


async def test_answer_question_closes_and_indexes_faq(memory_db, monkeypatch):
    """按编号回答后：写入 FAQ、关闭问题，后续同类提问直接命中 FAQ。"""
    qid = database.add_unanswered_question("g1", "这周有周赛吗", "s1")
    result = database.answer_unanswered_question("g1", qid, "本周日 19:00 机房", "teacher")
    assert result is not None and result["faq_id"]
    assert database.list_unanswered("g1") == []

    chat = AsyncMock(return_value="不应调用")
    monkeypatch.setattr(llm, "_chat_once", chat)
    monkeypatch.setattr(commands.db, "list_training_sessions", lambda *a, **k: [])
    monkeypatch.setattr(commands.db, "list_org_events", lambda *a, **k: [])
    monkeypatch.setattr(commands.db, "list_question_contests", lambda gid: [])
    monkeypatch.setattr(config, "QA_MEMBER_KB_ONLY", True)

    replies = await commands.handle_non_command_message(
        "周赛有没", "g1", "s2", staff=False
    )
    assert "本周日 19:00 机房" in replies[0]["text"]
    chat.assert_not_awaited()


# ----------------------------------------------------------------------
# 9) 能力类提问：固定话术，不能让模型臆造「我不能自动提醒」
# ----------------------------------------------------------------------
def test_capability_query_detected():
    for question in ("周赛你能自动提醒吗", "你能自动提醒吗", "机器人有什么功能", "你会做什么"):
        assert content_filter.is_capability_query(question)


def test_normal_question_is_not_capability_query():
    for question in ("这周有周赛吗", "这周有课吗", "比赛什么时候提醒"):
        assert not content_filter.is_capability_query(question)


async def test_capability_question_uses_fixed_text_without_model(monkeypatch):
    """能力类问题必须走固定话术，不产生模型调用（否则会说不能提醒）。"""
    chat = AsyncMock(return_value="我这边不能主动定时自动提醒")
    monkeypatch.setattr(llm, "_chat_once", chat)

    for staff in (True, False):
        replies = await commands.handle_non_command_message(
            "周赛你能自动提醒吗", "g1", "someone", staff=staff
        )
        assert replies[0]["text"] == commands.CAPABILITY_TEXT
        assert "自动提醒" in replies[0]["text"]
    chat.assert_not_awaited()


# ----------------------------------------------------------------------
# 10) 比赛通知范围：周赛只发集训队群，课程只发本群
# ----------------------------------------------------------------------
def _new_competition(name: str, hours_from_now: int) -> int:
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    target = (now + timedelta(hours=hours_from_now)).strftime("%Y-%m-%d %H:%M")
    info = {
        "name": name, "edition": "", "official_time": target,
        "official_reg_deadline": "", "official_site": "", "official_reg_start": "",
        "tracks": "", "official_rules": "", "device_req": "", "summary": "",
    }
    pid = database.create_pending_publication(info, "g1", "", "", "sys")
    cid, _ = database.approve_pending_publication(pid, "sys")
    return cid


def test_competition_without_scope_notifies_all_groups(memory_db, monkeypatch):
    import reminder

    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"TRAIN", "FRESH"})
    _new_competition("蓝桥杯", 5)
    comps = database.list_competitions(status="confirmed")
    from datetime import datetime
    from zoneinfo import ZoneInfo

    items = reminder.build_general_reminders(
        [], comps, datetime.now(ZoneInfo("Asia/Shanghai"))
    )
    assert {item["group_id"] for item in items} == {"TRAIN", "FRESH"}


def test_competition_scope_limits_reminder_groups(memory_db, monkeypatch):
    """周赛限定到集训队群后，新生群不应收到提醒。"""
    import reminder
    from datetime import datetime
    from zoneinfo import ZoneInfo

    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"TRAIN", "FRESH"})
    cid = _new_competition("ACM 周赛（本周）", 5)
    database.set_competition_notify_groups(cid, {"TRAIN"}, "admin")

    items = reminder.build_general_reminders(
        [], database.list_competitions(status="confirmed"),
        datetime.now(ZoneInfo("Asia/Shanghai")),
    )
    assert [item["group_id"] for item in items] == ["TRAIN"]


def test_scoped_competition_hidden_from_other_group_qa(memory_db, monkeypatch):
    """限定群的比赛不进入其它群的问答上下文。"""
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"TRAIN", "FRESH"})
    cid = _new_competition("ACM 周赛（本周）", 5)
    database.set_competition_notify_groups(cid, {"TRAIN"}, "admin")

    assert [c["name"] for c in database.list_question_contests("TRAIN")] == ["ACM 周赛（本周）"]
    assert database.list_question_contests("FRESH") == []


def test_scope_pointing_to_unknown_group_sends_nothing(memory_db, monkeypatch):
    """范围里全是未授权群时宁可不提醒，绝不退回全群广播。"""
    import reminder
    from datetime import datetime
    from zoneinfo import ZoneInfo

    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"TRAIN", "FRESH"})
    cid = _new_competition("ACM 周赛（本周）", 5)
    database.set_competition_notify_groups(cid, {"GONE"}, "admin")

    items = reminder.build_general_reminders(
        [], database.list_competitions(status="confirmed"),
        datetime.now(ZoneInfo("Asia/Shanghai")),
    )
    assert items == []


def test_publish_scope_defaults_to_source_group(memory_db, monkeypatch):
    """发布时默认只把发布群设进范围：两个群各自发布互不干扰。"""
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"TRAIN", "FRESH"})
    monkeypatch.setattr(config, "PUBLISH_SCOPE_TO_SOURCE", True)

    cid_train = _new_competition("ACM 周赛（本周）", 5)
    main._apply_publish_scope(cid_train, "TRAIN")
    cid_fresh = _new_competition("新生赛", 6)
    main._apply_publish_scope(cid_fresh, "FRESH")

    assert database.get_competition_notify_groups(cid_train) == {"TRAIN"}
    assert database.get_competition_notify_groups(cid_fresh) == {"FRESH"}

    # 问答上下文同样按群隔离
    assert [c["name"] for c in database.list_question_contests("TRAIN")] == ["ACM 周赛（本周）"]
    assert [c["name"] for c in database.list_question_contests("FRESH")] == ["新生赛"]


def test_publish_scope_can_be_disabled(memory_db, monkeypatch):
    """关闭开关后发布不再设范围（保持全群通知）。"""
    monkeypatch.setattr(config, "PUBLISH_SCOPE_TO_SOURCE", False)

    cid = _new_competition("蓝桥杯", 5)
    main._apply_publish_scope(cid, "TRAIN")
    assert database.get_competition_notify_groups(cid) == set()


def test_extend_scope_adds_second_group(memory_db, monkeypatch):
    """同一比赛在另一个群再发布时，只扩展范围，不新建记录。"""
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"TRAIN", "FRESH"})
    cid = _new_competition("蓝桥杯", 5)
    database.set_competition_notify_groups(cid, {"TRAIN"}, "admin")

    assert main._extend_scope_with_group(cid, "FRESH") is True
    assert database.get_competition_notify_groups(cid) == {"TRAIN", "FRESH"}
    # 重复扩展不再改变
    assert main._extend_scope_with_group(cid, "FRESH") is False


def test_training_reminder_stays_in_its_group(monkeypatch):
    """课程提醒只发所属群，不跨群。"""
    import reminder
    from datetime import datetime
    from zoneinfo import ZoneInfo

    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"TRAIN", "FRESH"})
    # 2026-11-01 是周日；课程安排在当天 19:00，落在 24h 提醒窗口内
    now = datetime(2026, 11, 1, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    session = {
        "id": 1, "group_id": "FRESH", "title": "新生集训课",
        "weekday": "周日", "start_time": "19:00", "end_time": "21:00",
        "location": "机房", "needs_laptop": 0, "software": "",
        "device_req": "", "training_group": "", "effective_start": "",
        "effective_end": "", "status": "confirmed",
    }
    items = reminder.build_training_reminders([session], now)
    assert items
    assert {item["group_id"] for item in items} == {"FRESH"}
