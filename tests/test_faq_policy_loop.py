"""队规 → FAQ 草稿闭环、私聊确认、直答来源提示与未答工单沉淀的离线回归。

覆盖三条链路：
1. 老师发规则通知 → 只写 pending 草稿 + 私聊确认卡，绝不直接生效；
2. 私聊/群内确认与驳回，含归属校验（不能替别人的草稿点头）；
3. 高置信 FAQ 直答带来源编号，以及「未答问题 → 一条命令转 confirmed FAQ」。
"""

import pytest

from cdutetc import commands, config, database, llm, teacher_intent
from cdutetc.handlers import faq_cmd


@pytest.fixture
def env(tmp_path, monkeypatch):
    """独立库 + 老师/超管白名单，且模型一律不出网。"""
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "policy.sqlite"))
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"t1"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"root"})
    monkeypatch.setattr(config, "TEACHER_POLICY_FAQ_ENABLED", True)
    monkeypatch.setattr(config, "QA_FAQ_LLM_ENABLED", False)
    monkeypatch.setattr(config, "QA_MEMBER_KB_ONLY", True)
    monkeypatch.setattr(config, "QA_FAQ_CITE_ENABLED", True)
    monkeypatch.setattr(config, "QA_SOCIAL_CHAT_ENABLED", False)
    monkeypatch.setattr(config, "LLM_API_KEY", "offline-test")
    monkeypatch.setattr(config, "QA_KB_MATCH_MIN_SCORE", 0.34)
    database.init_db()
    return database


# ---------------------------------------------------------------------
# 1. 队规提炼：只进草稿队列，草稿不参与作答
# ---------------------------------------------------------------------

async def test_policy_message_only_creates_pending_drafts(env, monkeypatch):
    async def fake_classify(text, *, group_id, now=None):
        return {"intent": teacher_intent.INTENT_POLICY_FAQ, "title": "考勤制度",
                "confidence": "high"}

    async def fake_extract(text):
        return [
            {"question": "训练迟到会怎样？", "answer": "记入考勤，迟到三次需补训。"},
            {"question": "请假怎么申请？", "answer": "提前一天在群里向负责老师请假。"},
        ]

    monkeypatch.setattr(teacher_intent, "is_teacher_speaker", lambda g, s: True)
    monkeypatch.setattr(teacher_intent, "classify", fake_classify)
    monkeypatch.setattr(llm, "extract_faqs", fake_extract)

    result = await teacher_intent.handle_message(
        "以后训练迟到三次要补训，请假要提前一天在群里说",
        group_id="g1", sender_id="t1",
    )

    assert result["action"] == "pending"
    # 群里只留一句轻量回执，细则走私聊
    assert "草稿" in result["reply"]
    notify = dict(result["notify_private"])
    assert "t1" in notify and "确认" in notify["t1"]

    drafts = env.list_pending_faq("g1")
    assert {d["question"] for d in drafts} == {"训练迟到会怎样？", "请假怎么申请？"}
    assert all(d["source"] == env.teacher_source("t1") for d in drafts)
    # 关键：未确认前不得生效，检索与作答都看不到
    assert env.list_faq("g1") == []


async def test_policy_message_without_drafts_stays_silent(env, monkeypatch):
    async def fake_classify(text, *, group_id, now=None):
        return {"intent": teacher_intent.INTENT_POLICY_FAQ, "title": "闲聊",
                "confidence": "medium"}

    async def fake_extract(text):
        return []

    monkeypatch.setattr(teacher_intent, "is_teacher_speaker", lambda g, s: True)
    monkeypatch.setattr(teacher_intent, "classify", fake_classify)
    monkeypatch.setattr(llm, "extract_faqs", fake_extract)

    result = await teacher_intent.handle_message(
        "大家最近状态不太好啊，多练练", group_id="g1", sender_id="t1")

    assert result["action"] == "ignored"
    assert not result["reply"] and not result["notify_private"]
    assert env.list_pending_faq("g1") == []


async def test_policy_message_dedupes_against_confirmed_faq(env, monkeypatch):
    existing = env.add_faq("g1", "请假怎么申请？", "提前一天说")

    async def fake_classify(text, *, group_id, now=None):
        return {"intent": teacher_intent.INTENT_POLICY_FAQ, "title": "请假制度",
                "confidence": "high"}

    async def fake_extract(text):
        return [{"question": "请假怎么申请？", "answer": "提前两天找负责老师。"}]

    monkeypatch.setattr(teacher_intent, "is_teacher_speaker", lambda g, s: True)
    monkeypatch.setattr(teacher_intent, "classify", fake_classify)
    monkeypatch.setattr(llm, "extract_faqs", fake_extract)

    result = await teacher_intent.handle_message(
        "请假制度改了，以后提前两天", group_id="g1", sender_id="t1")

    assert "未重复建档" in result["reply"]
    assert result["notify_private"] == []
    assert [item["id"] for item in env.list_faq("g1")] == [existing]


# ---------------------------------------------------------------------
# 2. 确认 / 驳回：私聊与群内两条入口，均校验归属
# ---------------------------------------------------------------------

def _draft(group_id: str = "g1", owner: str = "t1", question: str = "训练地点在哪？",
           answer: str = "实训楼 B302") -> int:
    return database.add_faq_pending(
        group_id, question, answer,
        source=database.teacher_source(owner), operator=owner,
    )["faq_id"]


def test_private_confirm_activates_draft(env):
    fid = _draft()
    lines = faq_cmd.handle_pending_decision("t1", f"确认 {fid}")

    assert lines and "已确认" in lines[0]
    assert env.get_faq_record(fid)["status"] == "confirmed"
    assert [item["id"] for item in env.list_faq("g1")] == [fid]


def test_private_reject_keeps_draft_out_of_answer_pool(env):
    fid = _draft()
    lines = faq_cmd.handle_pending_decision("t1", f"FAQ驳回 {fid}")

    assert lines and "已驳回" in lines[0]
    assert env.get_faq_record(fid)["status"] == "rejected"
    assert env.list_faq("g1") == []


def test_private_confirm_refuses_foreign_draft(env):
    fid = _draft(owner="t2")

    lines = faq_cmd.handle_pending_decision("t1", f"确认 {fid}")

    assert lines and "不是你的草稿" in lines[0]
    assert env.get_faq_record(fid)["status"] == "pending"


def test_super_admin_can_confirm_any_draft(env):
    fid = _draft(owner="t2")

    lines = faq_cmd.handle_pending_decision("root", f"确认 {fid}", allow_any=True)

    assert lines and "已确认" in lines[0]
    assert env.get_faq_record(fid)["status"] == "confirmed"


def test_private_chat_outside_grammar_is_untouched(env):
    _draft()
    # 日常闲聊、以及发布审批用的「同意/拒绝」词，都不该被 FAQ 决策抢走
    assert faq_cmd.handle_pending_decision("t1", "今天有训练吗") is None
    assert faq_cmd.handle_pending_decision("t1", "同意 3") is None
    assert faq_cmd.handle_pending_decision("t1", "驳回 3") is None


def test_group_commands_list_and_confirm_all(env):
    first = _draft(question="训练地点在哪？", answer="实训楼 B302")
    second = _draft(question="要自带电脑吗？", answer="需要，装好编译器")

    listed = faq_cmd.cmd_faq_pending_list("g1", "t1", "")[0]["text"]
    assert f"#{first}" in listed and f"#{second}" in listed

    confirmed = faq_cmd.cmd_faq_confirm("g1", "t1", "全部")[0]["text"]
    assert "2 条" in confirmed
    assert env.list_pending_faq("g1") == []
    assert {item["id"] for item in env.list_faq("g1")} == {first, second}


def test_group_commands_require_manager(env):
    _draft()

    assert "仅老师/超管" in faq_cmd.cmd_faq_pending_list("g1", "member", "")[0]["text"]
    assert "仅老师/超管" in faq_cmd.cmd_faq_confirm("g1", "member", "全部")[0]["text"]
    assert "仅老师/超管" in faq_cmd.cmd_faq_reject("g1", "member", "全部")[0]["text"]
    assert len(env.list_pending_faq("g1")) == 1


def test_pending_list_is_group_scoped(env):
    mine = _draft(group_id="g1")
    _draft(group_id="g2", question="外群的问题？")

    listed = faq_cmd.cmd_faq_pending_list("g1", "t1", "")[0]["text"]

    assert f"#{mine}" in listed
    assert "外群的问题" not in listed


# ---------------------------------------------------------------------
# 3. 直答来源提示 + 未答闭环
# ---------------------------------------------------------------------

async def test_high_confidence_answer_carries_source(env):
    fid = env.add_faq("g1", "训练地点在哪", "实训楼 B302")

    replies = await commands.handle_non_command_message("训练地点在哪", "g1", "member")

    assert replies[0]["text"].endswith(f"（依据 FAQ #{fid}）")


async def test_source_tag_can_be_disabled(env, monkeypatch):
    env.add_faq("g1", "训练地点在哪", "实训楼 B302")
    monkeypatch.setattr(config, "QA_FAQ_CITE_ENABLED", False)

    replies = await commands.handle_non_command_message("训练地点在哪", "g1", "member")

    assert replies[0]["text"] == "实训楼 B302"


def test_unanswered_question_dedupes_by_intent(env):
    first = env.add_unanswered_question("g1", "机房钥匙找谁", asker="m1")
    again = env.add_unanswered_question("g1", "机房钥匙找谁啊", asker="m2")

    assert again == first
    assert len(env.list_unanswered("g1")) == 1


def test_answering_question_closes_ticket_and_creates_faq(env):
    qid = env.add_unanswered_question("g1", "机房钥匙找谁", asker="m1")

    reply = commands.cmd_answer_question("g1", "t1", f"{qid} = 找当天值日的助教")[0]["text"]

    assert f"已回答 #{qid}" in reply
    assert env.list_unanswered("g1") == []
    faqs = env.list_faq("g1")
    assert len(faqs) == 1
    assert faqs[0]["answer"] == "找当天值日的助教"


def test_answering_question_requires_manager(env):
    env.add_unanswered_question("g1", "机房钥匙找谁", asker="m1")

    reply = commands.cmd_answer_question("g1", "member", "1 = 随便")[0]["text"]

    assert "仅老师/超管" in reply
    assert len(env.list_unanswered("g1")) == 1
