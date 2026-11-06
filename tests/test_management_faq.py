"""管理角色放行、FAQ 与同群同题问题的事务收尾回归。"""

from concurrent.futures import ThreadPoolExecutor
import sqlite3
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cdutetc.clients.onebot import OneBotMessage
import pytest

from cdutetc import commands
from cdutetc import config
from cdutetc import database
from cdutetc import main
from cdutetc.errors import DeliveryMode, DeliveryResult
from cdutetc import utils


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "management.sqlite"
    opened = []

    def connect():
        conn = sqlite3.connect(path, timeout=5, check_same_thread=False)
        opened.append(conn)
        return conn

    monkeypatch.setattr(database, "_connect", connect)
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    database.init_db()
    yield path
    for conn in opened:
        conn.close()


def message(api, text, member, group="g1"):
    return OneBotMessage({
        "message_id": 1, "group_id": group, "raw_message": f"[CQ:at,qq=30001] {text}",
        "message_type": "group", "user_id": member, "self_id": "30001",
    })


@pytest.mark.parametrize("member", ["admin", "teacher"])
async def test_onebot_role_can_add_cancel_courses_and_add_delete_faq(db, monkeypatch, member):
    question_id = database.add_unanswered_question("g1", "怎么报名？", "student")
    api = SimpleNamespace(send_group_msg_result=AsyncMock(
        return_value=DeliveryResult.sent("onebot", DeliveryMode.ACTIVE, "sent")
    ))
    client = main.ACMBot(api)
    await client.on_group_at_message_create(message(api, "/课程添加 算法课 周三 19:00-21:00 机房", member))
    course_id = database.list_training_sessions("g1")[0]["id"]
    assert "已添加课程" in api.send_group_msg_result.await_args.args[1]
    await client.on_group_at_message_create(message(api, f"/课程取消 {course_id}", member))
    assert database.list_training_sessions("g1") == []
    await client.on_group_at_message_create(message(api, "/FAQ添加 怎么报名？ = 联系老师", member))
    faq_id = database.list_faq("g1")[0]["id"]
    answered = database.list_unanswered("g1", "answered")
    assert [(q["id"], q["faq_id"]) for q in answered] == [(question_id, faq_id)]
    await client.on_group_at_message_create(message(api, f"/FAQ删除 {faq_id}", member))
    assert database.list_faq("g1") == []
    assert all(call.args[0] == "g1" for call in api.send_group_msg_result.await_args_list)


def test_management_helper_preserves_distinct_roles_and_default_denial(db, monkeypatch):
    assert utils.is_super_admin("admin") and not utils.is_teacher("admin")
    assert utils.can_manage_group("admin") and utils.can_manage_group("teacher")
    assert not utils.can_manage_group("student")
    monkeypatch.setattr(config, "SUPER_ADMIN", set())
    monkeypatch.setattr(config, "TEACHER_WHITELIST", set())
    assert not utils.can_manage_group("admin")
    assert not utils.can_manage_group("") and not utils.can_manage_group(None)


@pytest.mark.parametrize("command", [
    "/课程添加 算法课 周三 19:00-21:00 机房", "/课程取消 1",
    "/FAQ添加 怎么报名？ = 联系老师", "/FAQ删除 1", "/问题列表",
    "/抓取状态", "/提醒状态", "/抓取一次",
])
async def test_ordinary_member_management_is_denied_without_mutation(db, command):
    course = database.create_training_session("g1", {"title": "keep course"})
    faq = database.add_faq("g1", "keep question", "keep answer")
    database.add_unanswered_question("g1", "怎么报名？", "student")
    replies = await commands.handle_command(command, "g1", "student")
    assert "仅老师/超管" in replies[0]["text"]
    assert database.list_training_sessions("g1")[0]["id"] == course
    assert database.list_faq("g1")[0]["id"] == faq
    assert len(database.list_unanswered("g1")) == 1


async def test_super_admin_can_view_management_status_and_trigger_fetch(db, monkeypatch):
    fetch = AsyncMock(return_value=[{"kind": "disabled"}])
    monkeypatch.setattr(commands.crawler, "run_fetch", fetch)
    for command in ("/问题列表", "/抓取状态", "/提醒状态", "/抓取一次"):
        reply = await commands.handle_command(command, "g1", "admin")
        assert "仅老师" not in reply[0]["text"]
    fetch.assert_awaited_once()


async def test_admin_destructive_operations_still_filter_group(db):
    course = database.create_training_session("g2", {"title": "other course"})
    faq = database.add_faq("g2", "other question", "other answer")
    for command in (f"/课程取消 {course}", f"/FAQ删除 {faq}"):
        reply = await commands.handle_command(command, "g1", "admin")
        assert "不属于本群" in reply[0]["text"]
    assert database.list_training_sessions("g2")[0]["id"] == course
    assert database.list_faq("g2")[0]["id"] == faq


def test_faq_closes_only_same_group_whitespace_normalized_exact_questions(db):
    matching = database.add_unanswered_question("g1", "  怎么\t安装  VSCode？ \n", "student")
    assert database.add_unanswered_question("g1", "怎么 安装 VSCode？", "another") == matching
    with database._connect() as conn:
        unrelated = conn.execute(
            "INSERT INTO unanswered_questions (group_id, question, asker, asked_at) VALUES ('g1', ?, 'student', '')",
            ("怎么 安装 VSCode？还要配置什么",),
        ).lastrowid
        case_different = conn.execute(
            "INSERT INTO unanswered_questions (group_id, question, asker, asked_at) VALUES ('g1', ?, 'student', '')",
            ("怎么 安装 vscode？",),
        ).lastrowid
        punctuation_different = conn.execute(
            "INSERT INTO unanswered_questions (group_id, question, asker, asked_at) VALUES ('g1', ?, 'student', '')",
            ("怎么 安装 VSCode?",),
        ).lastrowid
        other_group = conn.execute(
            "INSERT INTO unanswered_questions (group_id, question, asker, asked_at) VALUES ('g2', ?, 'student', '')",
            ("怎么 安装 VSCode？",),
        ).lastrowid
    # 直接插入模拟语义去重上线前已存在的历史行，专测 FAQ 仅关闭规范化后精确同题。
    with database._connect() as conn:
        legacy = conn.execute(
            "INSERT INTO unanswered_questions (group_id, question, asker, asked_at) VALUES ('g1', ?, 'legacy', '')",
            ("\n怎么  安装\tVSCode？ ",),
        ).lastrowid
    faq_id = database.add_faq("g1", " 怎么\n安装 VSCode？ ", "答案")
    assert database.list_faq("g1")[0]["question"] == "怎么 安装 VSCode？"
    assert {q["id"] for q in database.list_unanswered("g1")} == {unrelated, case_different, punctuation_different}
    assert database.list_unanswered("g2")[0]["id"] == other_group
    assert {q["id"] for q in database.list_unanswered("g1", "answered")} == {matching, legacy}
    assert all(q["faq_id"] == faq_id for q in database.list_unanswered("g1", "answered"))


def test_unanswered_question_ingestion_merges_semantically_similar_questions(db):
    first = database.add_unanswered_question("g1", "怎么报名？")
    assert database.add_unanswered_question("g1", "怎么报名？要组队吗") == first
    assert [q["id"] for q in database.list_unanswered("g1")] == [first]


def test_repeated_faq_addition_does_not_relink_answered_or_close_similar_question(db):
    qid = database.add_unanswered_question("g1", "怎么报名？")
    # 模拟语义合并上线前的独立历史题，专门验证重复 FAQ 不扩大关闭范围。
    with database._connect() as conn:
        similar = conn.execute(
            "INSERT INTO unanswered_questions (group_id, question, asker, asked_at) VALUES ('g1', ?, 'student', '')",
            ("怎么报名？要组队吗",),
        ).lastrowid
    first = database.add_faq("g1", "怎么报名？", "联系老师")
    second = database.add_faq("g1", "怎么报名？", "找老师报名")
    assert second != first  # 保留现有添加 FAQ 的语义。
    assert database.list_unanswered("g1", "answered")[0]["faq_id"] == first
    assert database.list_unanswered("g1", "answered")[0]["id"] == qid
    assert database.list_unanswered("g1")[0]["id"] == similar


@pytest.mark.parametrize("failure", ["question-update", "audit"])
def test_faq_and_question_state_roll_back_together(db, monkeypatch, failure):
    qid = database.add_unanswered_question("g1", "怎么报名？")
    if failure == "question-update":
        with database._connect() as conn:
            conn.execute("""CREATE TRIGGER fail_resolve BEFORE UPDATE OF status ON unanswered_questions
                WHEN NEW.status = 'answered' BEGIN SELECT RAISE(ABORT, 'injected resolve failure'); END""")
    else:
        def failed_audit(*args, **kwargs):
            raise sqlite3.OperationalError("injected audit failure")
        monkeypatch.setattr(database, "add_audit", failed_audit)
    with pytest.raises(sqlite3.Error):
        database.add_faq("g1", "怎么报名？", "联系老师")
    assert database.list_faq("g1") == []
    row = database.list_unanswered("g1")[0]
    assert row["id"] == qid and row["faq_id"] == 0
    assert database.list_unanswered("g1", "answered") == []


def test_independent_faq_writers_resolve_a_question_once(db):
    qid = database.add_unanswered_question("g1", "怎么报名？")
    gate = Barrier(2)

    def add(answer):
        gate.wait(timeout=5)
        return database.add_faq("g1", "怎么报名？", answer)

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(add, ["answer A", "answer B"]))
    assert database.list_unanswered("g1") == []
    answered = database.list_unanswered("g1", "answered")
    assert len(answered) == 1 and answered[0]["id"] == qid
    assert answered[0]["faq_id"] in ids
