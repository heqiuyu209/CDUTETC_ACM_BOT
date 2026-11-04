"""未补零旧日期的截止判断、真实提醒去重及成功事实迁移回归。"""

from datetime import datetime
import sqlite3
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

import commands
import config
import database
import reminder

RAW = "2026-11-01 9:00"
TARGET = "2026-11-01 09:00"
NOW = datetime(2026, 11, 1, 8, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def db(tmp_path, monkeypatch):
    connections = []
    path = tmp_path / "dates.sqlite"

    def connect():
        conn = sqlite3.connect(path)
        connections.append(conn)
        return conn

    monkeypatch.setattr(database, "_connect", connect)
    monkeypatch.setattr(database, "now_str", lambda: "2026-11-01 08:00:00")
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    database.init_db()
    yield path
    for conn in connections:
        conn.close()


async def test_command_with_unpadded_hour_cannot_register_after_deadline(db, monkeypatch):
    created = await commands.handle_command("/征集 截止测试 2026-11-01 9:00 10 name", "g1", "teacher")
    assert "已发起征集" in created[0]["text"]
    collection = database.list_collections("g1")[0]
    assert collection["deadline"] == TARGET
    monkeypatch.setattr(database, "now_str", lambda: "2026-11-01 10:00:00")
    reply = await commands.handle_command(f"/报名 {collection['id']} name=Alice", "g1", "member")
    assert reply[0]["text"] == "已超过征集截止时间"
    assert database.summarize_collection(collection["id"])["registered"] == 0


@pytest.mark.parametrize("restart", [False, True])
def test_preexisting_unpadded_deadline_is_compared_as_datetime(db, monkeypatch, restart):
    cid = database.create_collection("g1", "旧征集", deadline=TARGET)
    with database._connect() as conn:
        conn.execute("UPDATE collections SET deadline = ? WHERE id = ?", ("2026-11-1 9:0", cid))
    if restart:
        database.init_db()
        assert database.get_collection(cid)["deadline"] == TARGET
    monkeypatch.setattr(database, "now_str", lambda: "2026-11-01 10:00:00")
    assert database.register_collection(cid, "g1", "member") == (False, "已超过征集截止时间")
    assert database.summarize_collection(cid)["registered"] == 0


def test_invalid_preexisting_deadline_is_retained_and_rejects_registration(db):
    cid = database.create_collection("g1", "旧征集", deadline=TARGET)
    with database._connect() as conn:
        conn.execute("UPDATE collections SET deadline = '下周末' WHERE id = ?", (cid,))
    database.init_db()
    assert database.get_collection(cid)["deadline"] == "下周末"
    assert database.register_collection(cid, "g1", "member") == (False, "征集截止时间无效，请联系老师")


async def test_amending_padding_keeps_success_history_but_changed_time_sends_once(db):
    cid = database.create_competition({"name": "同一时刻", "official_time": RAW}, status="confirmed")
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW) == 1
    amended = await commands.handle_command(f"/修正 {cid} time={TARGET}", "g1", "admin")
    assert "已修正" in amended[0]["text"]
    assert await reminder.check_general_reminders(sender, now=NOW) == 0
    assert sender.await_count == 1
    assert len(database.list_deliveries("g1")) == 1
    database.amend_competition(cid, {"official_time": "2026-11-01 10:00"})
    assert await reminder.check_general_reminders(sender, now=NOW) == 1
    database.amend_competition(cid, {"official_time": RAW})
    assert await reminder.check_general_reminders(sender, now=NOW) == 0
    assert sender.await_count == 2


@pytest.mark.parametrize("old_status, has_receipt", [("sent", False), ("sending", True), ("cancelled", True)])
async def test_migration_merges_equivalent_delivery_rows_and_retains_success(db, old_status, has_receipt):
    cid = database.create_competition({"name": "旧比赛", "official_time": TARGET}, status="confirmed")
    old_key = f"g1:competition:{cid}:contest_time:{RAW}"
    key = database.delivery_dedup_key("g1", "competition", cid, "contest_time", TARGET)
    with database._connect() as conn:
        conn.execute("UPDATE competitions SET official_time = ? WHERE id = ?", (RAW, cid))
        conn.execute("""INSERT INTO reminder_deliveries
            (group_id, obj_type, obj_id, remind_type, target_time, attempts, status, sent_at, dedup_key, lease_token)
            VALUES ('g1', 'competition', ?, 'contest_time', ?, 1, ?, ?, ?, 'old-token')""",
                     (cid, RAW, old_status, "2026-11-01 08:00:00" if old_status == "sent" else "", old_key))
        conn.execute("""INSERT INTO reminder_deliveries
            (group_id, obj_type, obj_id, remind_type, target_time, attempts, dedup_key)
            VALUES ('g1', 'competition', ?, 'contest_time', ?, 3, ?)""", (cid, TARGET, key))
        if has_receipt:
            conn.execute("INSERT INTO reminder_success_receipts VALUES (?, 'old-token', '2026-11-01 08:00:00')", (old_key,))
    database.init_db()
    database.init_db()
    assert database.get_competition(cid)["official_time"] == TARGET
    row = database.get_delivery_by_key(old_key)
    assert (row["dedup_key"], row["target_time"], row["status"], row["attempts"]) == (key, TARGET, "sent", 3)
    assert row["sent_at"] == "2026-11-01 08:00:00"
    assert len(database.list_deliveries("g1")) == 1
    if has_receipt:
        with database._connect() as conn:
            # 原始成功事实保持原样，规范身份同样指向原成功回执。
            assert conn.execute("SELECT lease_token, sent_at FROM reminder_success_receipts WHERE dedup_key = ?", (old_key,)).fetchone() == ("old-token", "2026-11-01 08:00:00")
            assert conn.execute("SELECT lease_token FROM reminder_success_receipts WHERE dedup_key = ?", (key,)).fetchone() == ("old-token",)
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW) == 0
    sender.assert_not_awaited()


async def test_orphan_success_receipt_still_prevents_resend_after_normalization(db):
    cid = database.create_competition({"name": "成功状态曾写库失败", "official_time": TARGET}, status="confirmed")
    old_key = f"g1|competition|{cid}|contest_time|{RAW}"
    with database._connect() as conn:
        conn.execute("INSERT INTO reminder_success_receipts VALUES (?, 'old-token', '2026-11-01 08:00:00')", (old_key,))
    database.init_db()
    sender = AsyncMock(return_value=True)
    assert await reminder.check_general_reminders(sender, now=NOW) == 0
    sender.assert_not_awaited()
    assert database.get_delivery_by_key(old_key)["status"] == "sent"


async def test_legacy_success_marks_merge_and_padding_does_not_resend(db, monkeypatch):
    cid = database.save_contest("g1", {"name": "旧比赛", "contest_time": RAW})
    with database._connect() as conn:
        conn.execute("UPDATE contests SET contest_time = ? WHERE id = ?", (RAW, cid))
        for target, sent in [(RAW, "2026-11-01 07:30:00"), (TARGET, "2026-11-01 07:40:00")]:
            conn.execute("INSERT INTO reminders (group_id, contest_id, remind_type, target_time, sent_at) VALUES ('g1', ?, 'contest_time', ?, ?)", (cid, target, sent))
    database.init_db()
    database.init_db()
    assert database.list_contests("g1")[0]["contest_time"] == TARGET
    assert database.has_reminded("g1", cid, "contest_time", RAW)
    assert not database.mark_reminded("g1", cid, "contest_time", "2026-11-1 9:0")
    with database._connect() as conn:
        assert conn.execute("SELECT target_time, sent_at FROM reminders").fetchall() == [(TARGET, "2026-11-01 07:30:00")]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(reminder, "datetime", Clock)
    sender = AsyncMock(return_value=True)
    assert await reminder.check_reminders(sender) == 0
    sender.assert_not_awaited()


def test_normalization_preserves_nonzero_legacy_seconds(db):
    key = database.delivery_dedup_key("g1", "collection", 1, "deadline", TARGET)
    assert database.delivery_dedup_key("g1", "collection", 1, "deadline", "2026/11/1 9:0") == key
    assert database.delivery_dedup_key("g1", "collection", 1, "deadline", "2026-11-01 09:00:00") == key
    assert database.delivery_dedup_key("g1", "collection", 1, "deadline", "2026-11-01 09:00:01") != key


def test_migration_normalizes_operational_dates_without_rewriting_audit_history(db):
    cid = database.create_competition({"name": "旧比赛", "official_time": TARGET}, operator="admin")
    with database._connect() as conn:
        versions = conn.execute("SELECT fields_json FROM competition_versions").fetchall()
        audits = conn.execute("SELECT before_json, after_json FROM audit_log").fetchall()
        conn.execute("UPDATE competitions SET official_reg_start = ?, official_reg_deadline = ?, official_time = ? WHERE id = ?", (RAW, RAW, RAW, cid))
        conn.execute("""INSERT INTO org_events (title, reg_deadline, contest_time, effective_start, effective_end)
            VALUES ('旧校安排', ?, ?, ?, ?)""", (RAW, RAW, RAW, RAW))
        conn.execute("INSERT INTO training_sessions (group_id, title, effective_start, effective_end) VALUES ('g1', '旧课程', ?, ?)", (RAW, RAW))
    database.init_db()
    with database._connect() as conn:
        assert conn.execute("SELECT official_reg_start, official_reg_deadline, official_time FROM competitions WHERE id = ?", (cid,)).fetchone() == (TARGET, TARGET, TARGET)
        assert conn.execute("SELECT reg_deadline, contest_time, effective_start, effective_end FROM org_events").fetchone() == (TARGET, TARGET, TARGET, TARGET)
        assert conn.execute("SELECT effective_start, effective_end FROM training_sessions").fetchone() == (TARGET, TARGET)
        assert conn.execute("SELECT fields_json FROM competition_versions").fetchall() == versions
        assert conn.execute("SELECT before_json, after_json FROM audit_log").fetchall() == audits


def test_business_date_migration_is_atomic_when_an_update_fails(db):
    cid = database.create_competition({"name": "旧比赛", "official_time": TARGET})
    collection = database.create_collection("g1", "旧征集", deadline=TARGET)
    with database._connect() as conn:
        conn.execute("UPDATE competitions SET official_time = ? WHERE id = ?", (RAW, cid))
        conn.execute("UPDATE collections SET deadline = ? WHERE id = ?", (RAW, collection))
        conn.execute("""CREATE TRIGGER reject_normalized_deadline BEFORE UPDATE OF deadline ON collections
            BEGIN SELECT RAISE(ABORT, 'date migration rejected'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="date migration rejected"):
        database.init_db()
    assert database.get_competition(cid)["official_time"] == RAW
    assert database.get_collection(collection)["deadline"] == RAW
