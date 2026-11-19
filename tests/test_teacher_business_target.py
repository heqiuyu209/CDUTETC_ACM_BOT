"""模型改期目标以带类型 ID、本群归属、当前状态及当前老师权限复核。"""

import sqlite3
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock

import pytest

from cdutetc import business_followup, config, database as db, group_cleanup, teacher_intent

NOW = datetime(2026, 11, 19, 9)
TIME = "2026-11-19 19:00"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "teacher-target.sqlite3"))
    monkeypatch.setattr(db, "_delivery_now", lambda value=None: value or NOW.replace(tzinfo=ZoneInfo("Asia/Shanghai")))
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher", "other"})
    db.init_db()


def _both():
    tid = db.create_training_session("g1", {"title": "集训", "weekday": "周四", "start_time": "19:00", "end_time": "21:00"})
    cid = db.create_competition({"name": "校赛", "official_time": TIME, "summary": "共享说明"}, status="confirmed")
    assert tid == cid
    return tid, cid


def _apply(obj_id, target_type="", sender="teacher", **changes):
    return teacher_intent._apply_update("g1", {"target_id": obj_id, "target_type": target_type,
                                             "location": "93501", **changes}, NOW, sender)


def test_colliding_id_without_type_is_rejected(env):
    tid, cid = _both()
    course, contest = db.get_group_course("g1", tid), db.get_competition(cid)
    assert _apply(tid) is None
    assert db.get_group_course("g1", tid) == course
    assert db.get_competition(cid) == contest


@pytest.mark.parametrize("target_type", ["course", "training_session"])
def test_explicit_course_does_not_update_colliding_competition(env, target_type):
    tid, cid = _both()
    before = db.get_competition(cid)
    assert _apply(tid, target_type)[0] == "course_updated"
    assert db.get_group_course("g1", tid)["location"] == "93501"
    assert db.get_competition(cid) == before
    assert not db.list_group_competitions("g1")[0].get("location")


@pytest.mark.parametrize("target_type", ["contest", "competition"])
def test_explicit_competition_uses_only_group_location_overlay(env, target_type):
    tid, cid = _both()
    course, contest = db.get_group_course("g1", tid), db.get_competition(cid)
    assert _apply(cid, target_type)[0] == "contest_updated"
    assert db.get_group_course("g1", tid) == course
    assert db.get_competition(cid) == contest
    assert db.list_group_competitions("g1")[0]["location"] == "93501"
    assert not db.list_group_competitions("g2")[0].get("location")


@pytest.mark.parametrize("kind", ["competition", "training_session"])
def test_draft_target_is_rejected(env, kind):
    if kind == "competition":
        oid = db.create_competition({"name": "待审", "official_time": TIME})
        before = db.get_competition(oid)
    else:
        oid = db.create_training_session("g1", {"title": "待审", "weekday": "周四", "start_time": "19:00", "status": "draft"})
        before = db.get_group_course("g1", oid)
    assert _apply(oid, kind) is None
    assert (db.get_competition(oid) if kind == "competition" else db.get_group_course("g1", oid)) == before


def test_non_owner_global_teacher_cannot_update_assigned_group(env):
    tid, _ = _both()
    db.set_group_teachers("g1", ["teacher"], operator="admin")
    before = db.get_group_course("g1", tid)
    assert _apply(tid, "course", sender="other") is None
    assert db.get_group_course("g1", tid) == before
    assert _apply(tid, "course", sender="teacher")[0] == "course_updated"


def test_teacher_removal_is_rechecked_after_classification(env, monkeypatch):
    tid, _ = _both()
    db.set_group_teachers("g1", ["teacher"], operator="admin")
    assert teacher_intent.is_teacher_speaker("g1", "teacher")
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"other"})
    before = db.get_group_course("g1", tid)
    assert _apply(tid, "course") is None
    assert db.get_group_course("g1", tid) == before


def test_teacher_reassignment_is_rechecked_after_classification(env):
    tid, _ = _both()
    db.set_group_teachers("g1", ["teacher"], operator="admin")
    assert teacher_intent.is_teacher_speaker("g1", "teacher")
    db.set_group_teachers("g1", ["other"], operator="admin")
    before = db.get_group_course("g1", tid)
    assert _apply(tid, "course") is None
    assert db.get_group_course("g1", tid) == before


@pytest.mark.parametrize("kind", ["competition", "training_session"])
def test_foreign_group_target_is_rejected(env, kind):
    if kind == "competition":
        oid = db.create_competition({"name": "另一群校赛", "official_time": TIME}, status="confirmed")
        db.set_competition_notify_groups(oid, {"g2"})
    else:
        oid = db.create_training_session("g2", {"title": "另一群集训", "weekday": "周四", "start_time": "19:00"})
    assert _apply(oid, kind) is None


def test_archived_target_is_not_restored_or_modified(env):
    _, cid = _both()
    group_cleanup.archive_competition("g1", cid, operator="admin")
    before = db.get_competition(cid)
    assert _apply(cid, "competition") is None
    assert db.get_competition(cid) == before
    assert db.get_competition_target_groups(cid) == {"g2"}


def test_superadmin_can_update_assigned_group_without_teacher_membership(env):
    tid, _ = _both()
    db.set_group_teachers("g1", ["teacher"], operator="admin")
    assert _apply(tid, "course", sender="admin")[0] == "course_updated"


def _business_snapshot():
    tables = ("training_sessions", "teacher_intent_pending", "group_faq", "competitions", "competition_versions", "reminder_deliveries",
              "competition_notify_groups", "group_competition_locations", "audit_log",
              "group_competition_archives", "sqlite_sequence")
    with closing(db._connect()) as conn:
        return {table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in tables}


@pytest.mark.parametrize("reject", ["fields", "overlay", "version", "audit"])
def test_combined_competition_fields_and_location_roll_back_atomically(env, reject):
    _, cid = _both()
    trigger = {
        "fields": "BEFORE UPDATE OF official_time ON competitions",
        "overlay": "BEFORE INSERT ON group_competition_locations",
        "version": "BEFORE INSERT ON competition_versions",
        "audit": "BEFORE INSERT ON audit_log",
    }[reject]
    with closing(db._connect()) as conn, conn:
        conn.execute(f"CREATE TRIGGER offline_reject {trigger} BEGIN SELECT RAISE(ABORT, 'offline failure'); END")
    before = _business_snapshot()
    with pytest.raises(sqlite3.IntegrityError, match="offline failure"):
        _apply(cid, "competition", date="2026-11-20", start_time="20:00")
    assert _business_snapshot() == before


def test_invalid_competition_location_does_not_commit_other_fields(env):
    _, cid = _both()
    before = _business_snapshot()
    assert _apply(cid, "competition", location="x" * 41, date="2026-11-20", start_time="20:00") is None
    assert _business_snapshot() == before


def test_update_competition_borrowed_connection_does_not_commit(env):
    _, cid = _both()
    before = _business_snapshot()
    with closing(db._connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        assert db.update_competition(cid, {"official_time": "2026-11-20 20:00"}, conn=conn)
        assert conn.in_transaction
        assert conn.execute("SELECT official_time FROM competitions WHERE id=?", (cid,)).fetchone() == ("2026-11-20 20:00",)
        conn.rollback()
    assert _business_snapshot() == before


@pytest.mark.parametrize("change", ["owner", "scope", "status", "archive", "new_course_collision"])
@pytest.mark.parametrize("cancel", [False, True])
def test_competition_permission_and_scope_rechecked_inside_write_transaction(env, monkeypatch, change, cancel):
    cid = db.create_competition({"name": "校赛", "official_time": TIME}, status="confirmed")
    db.set_group_teachers("g1", ["teacher"], operator="admin")
    original = db.get_competition_target_groups
    raced = False
    after_race = None

    def targets_with_race(competition_id):
        nonlocal raced, after_race
        targets = original(competition_id)
        if not raced:
            raced = True
            if change == "owner":
                db.set_group_teachers("g1", ["other"], operator="admin")
            elif change == "scope":
                db.set_competition_notify_groups(cid, {"g2"}, operator="admin")
            elif change == "status":
                db.set_competition_status(cid, "draft", operator="admin")
            elif change == "archive":
                group_cleanup.archive_competition("g1", cid, operator="admin")
            else:
                tid = db.create_training_session("g1", {"title": "新集训", "weekday": "周四", "start_time": "19:00"})
                assert tid == cid
            after_race = _business_snapshot()
        return targets

    monkeypatch.setattr(db, "get_competition_target_groups", targets_with_race)
    target_type = "" if change == "new_course_collision" else "competition"
    assert _apply(cid, target_type, cancel=cancel, date="2026-11-20", start_time="20:00") is None
    assert raced and _business_snapshot() == after_race


def test_combined_competition_change_commits_fields_and_group_overlay(env):
    _, cid = _both()
    assert _apply(cid, "competition", date="2026-11-20", start_time="20:00")[0] == "contest_updated"
    assert db.get_competition(cid)["official_time"] == "2026-11-20 20:00"
    assert db.list_group_competitions("g1")[0]["location"] == "93501"
    assert not db.list_group_competitions("g2")[0]["location"]


def _save_contest(**changes):
    return teacher_intent._save_contest("g1", {"title": "本群校赛", "date": "2026-11-19",
                                              "start_time": "19:00", "location": "93501",
                                              **changes}, NOW, "teacher")


@pytest.mark.parametrize("location", ["", "93501"])
def test_new_competition_commits_only_this_group_scope_and_location(env, location):
    contest = _save_contest(location=location)
    assert contest["name"] == "本群校赛"
    assert contest["official_time"] == TIME
    assert contest["location"] == location
    assert db.get_competition_target_groups(contest["id"]) == {"g1"}
    assert not db.list_group_competitions("g2")
    with closing(db._connect()) as conn:
        pending = conn.execute("SELECT DISTINCT group_id FROM reminder_deliveries WHERE status='pending'").fetchall()
    assert pending == [("g1",)]


@pytest.mark.parametrize("reject", ["competition", "version", "scope", "reminder", "overlay", "scope_audit", "location_audit"])
def test_new_competition_rolls_back_every_write_when_one_step_fails(env, reject):
    trigger = {
        "competition": "BEFORE INSERT ON competitions",
        "version": "BEFORE INSERT ON competition_versions",
        "scope": "BEFORE INSERT ON competition_notify_groups",
        "reminder": "BEFORE INSERT ON reminder_deliveries",
        "overlay": "BEFORE INSERT ON group_competition_locations",
        "scope_audit": "BEFORE INSERT ON audit_log WHEN NEW.action='set_notify_groups'",
        "location_audit": "BEFORE INSERT ON audit_log WHEN NEW.action='location_followup'",
    }[reject]
    with closing(db._connect()) as conn, conn:
        conn.execute(f"CREATE TRIGGER offline_reject {trigger} BEGIN SELECT RAISE(ABORT, 'offline failure'); END")
    before = _business_snapshot()
    assert _save_contest() is None
    assert _business_snapshot() == before


def test_new_competition_with_invalid_location_is_not_partially_saved(env):
    before = _business_snapshot()
    assert _save_contest(location="x" * 41) is None
    assert _business_snapshot() == before


@pytest.mark.parametrize("reject", ["scope", "location", "fresh"])
def test_new_competition_false_result_rolls_back_previous_writes(env, monkeypatch, reject):
    if reject == "scope":
        monkeypatch.setattr(db, "set_competition_notify_groups", lambda *args, **kwargs: 0)
    elif reject == "location":
        monkeypatch.setattr(business_followup, "set_competition_location", lambda *args, **kwargs: False)
    else:
        monkeypatch.setattr(business_followup, "_record", lambda *args: None)
    before = _business_snapshot()
    assert _save_contest() is None
    assert _business_snapshot() == before


@pytest.mark.parametrize("change", ["owner", "whitelist", "group"])
def test_new_competition_rechecks_authorization_at_transaction_start(env, monkeypatch, change):
    db.set_group_teachers("g1", ["teacher"], operator="admin")
    assert teacher_intent.is_teacher_speaker("g1", "teacher")
    original = teacher_intent._slot_date
    after_race = None

    def date_with_race(slots, now):
        nonlocal after_race
        moment = original(slots, now)
        if change == "owner":
            db.set_group_teachers("g1", ["other"], operator="admin")
        elif change == "whitelist":
            monkeypatch.setattr(config, "TEACHER_WHITELIST", {"other"})
        else:
            monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g2"})
        after_race = _business_snapshot()
        return moment

    monkeypatch.setattr(teacher_intent, "_slot_date", date_with_race)
    assert _save_contest() is None
    assert _business_snapshot() == after_race


def test_create_and_scope_borrowed_connection_do_not_commit(env):
    before = _business_snapshot()
    with closing(db._connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        cid = db.create_competition({"name": "未提交比赛", "official_time": TIME},
                                    status="confirmed", conn=conn)
        assert db.set_competition_notify_groups(cid, {"g1"}, conn=conn) == 1
        assert conn.in_transaction
        assert conn.execute("SELECT group_id FROM competition_notify_groups WHERE competition_id=?", (cid,)).fetchall() == [("g1",)]
        with closing(db._connect()) as observer:
            assert not observer.execute("SELECT id FROM competitions").fetchall()
        conn.rollback()
    assert _business_snapshot() == before


def _course_with_reminders():
    tid = db.create_training_session("g1", {"title": "集训", "weekday": "周四", "start_time": "19:00",
                                           "end_time": "21:00", "location": "旧地点"})
    for status, target in [("pending", TIME), ("sent", "2026-11-12 19:00"), ("sending", "2026-11-26 19:00")]:
        db.ensure_delivery("g1", "training_session", tid, "start_time", target)
        if status != "pending":
            with closing(db._connect()) as conn, conn:
                conn.execute("UPDATE reminder_deliveries SET status=? WHERE group_id='g1' AND target_time=?", (status, target))
    db.ensure_delivery("g2", "training_session", tid, "start_time", TIME)
    return tid


@pytest.mark.parametrize("change", ["owner", "whitelist", "draft", "cancelled", "group", "new_competition_collision"])
@pytest.mark.parametrize("cancel", [False, True])
def test_course_permission_and_target_rechecked_inside_write_transaction(env, monkeypatch, change, cancel):
    tid = _course_with_reminders()
    db.set_group_teachers("g1", ["teacher"], operator="admin")
    original = teacher_intent._parse_weekday
    raced = False
    after_race = None

    def weekday_with_race(value):
        nonlocal raced, after_race
        weekday = original(value)
        if not raced:
            raced = True
            if change == "owner":
                db.set_group_teachers("g1", ["other"], operator="admin")
            elif change == "whitelist":
                monkeypatch.setattr(config, "TEACHER_WHITELIST", {"other"})
            elif change in {"draft", "cancelled"}:
                db.update_group_course("g1", tid, "admin", status=change)
            elif change == "group":
                with closing(db._connect()) as conn, conn:
                    conn.execute("UPDATE training_sessions SET group_id='g2' WHERE id=?", (tid,))
            else:
                cid = db.create_competition({"name": "新比赛", "official_time": TIME}, status="confirmed")
                assert cid == tid
            after_race = _business_snapshot()
        return weekday

    monkeypatch.setattr(teacher_intent, "_parse_weekday", weekday_with_race)
    target_type = "" if change == "new_competition_collision" else "course"
    assert _apply(tid, target_type, cancel=cancel) is None
    assert raced and _business_snapshot() == after_race


@pytest.mark.parametrize("reject", ["fields", "audit", "pending"])
@pytest.mark.parametrize("cancel", [False, True])
def test_course_update_or_cancel_rolls_back_fields_audit_and_reminder_cleanup(env, reject, cancel):
    tid = _course_with_reminders()
    trigger = {
        "fields": "BEFORE UPDATE ON training_sessions",
        "audit": "BEFORE INSERT ON audit_log",
        "pending": "BEFORE DELETE ON reminder_deliveries",
    }[reject]
    with closing(db._connect()) as conn, conn:
        conn.execute(f"CREATE TRIGGER offline_reject {trigger} BEGIN SELECT RAISE(ABORT, 'offline failure'); END")
    before = _business_snapshot()
    with pytest.raises(sqlite3.IntegrityError, match="offline failure"):
        _apply(tid, "course", cancel=cancel)
    assert _business_snapshot() == before


@pytest.mark.parametrize("cancel", [False, True])
def test_course_update_and_cancel_commit_only_this_groups_pending_cleanup(env, cancel):
    tid = _course_with_reminders()
    assert _apply(tid, "course", cancel=cancel)[0] == ("course_cancelled" if cancel else "course_updated")
    course = db.get_group_course("g1", tid)
    assert course["status"] == ("cancelled" if cancel else "confirmed")
    assert course["location"] == ("旧地点" if cancel else "93501")
    with closing(db._connect()) as conn:
        nodes = conn.execute("SELECT group_id,status FROM reminder_deliveries ORDER BY group_id,status").fetchall()
    assert nodes == [("g1", "sending"), ("g1", "sent"), ("g2", "pending")]


@pytest.mark.parametrize("cancel", [False, True])
def test_course_write_false_result_rolls_back_changes(env, monkeypatch, cancel):
    tid = _course_with_reminders()
    name = "cancel_training_session" if cancel else "update_group_course"
    original = getattr(db, name)

    def false_after_write(*args, **kwargs):
        assert original(*args, **kwargs)
        return False

    monkeypatch.setattr(db, name, false_after_write)
    before = _business_snapshot()
    assert _apply(tid, "course", cancel=cancel) is None
    assert _business_snapshot() == before


@pytest.mark.parametrize("cancel", [False, True])
def test_course_write_and_pending_cleanup_borrowed_connection_do_not_commit(env, cancel):
    tid = _course_with_reminders()
    before = _business_snapshot()
    with closing(db._connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if cancel:
            assert db.cancel_training_session("g1", tid, "teacher", conn=conn)
        else:
            assert db.update_group_course("g1", tid, "teacher", conn=conn, location="93501")
        assert db.drop_pending_deliveries("training_session", tid, group_id="g1", conn=conn) == 1
        assert conn.in_transaction
        assert _business_snapshot() == before
        conn.rollback()
    assert _business_snapshot() == before


def _save_course(intent=teacher_intent.INTENT_RECURRING_COURSE):
    return teacher_intent._save_course("g1", intent, {"title": "新集训", "date": "2026-11-19",
                                                       "weekday": "周四", "start_time": "19:00",
                                                       "end_time": "21:00", "location": "93501"}, NOW, "teacher")


@pytest.mark.parametrize("intent", [teacher_intent.INTENT_RECURRING_COURSE, teacher_intent.INTENT_ONE_OFF_SESSION])
def test_new_course_commits_with_group_scope_and_effective_dates(env, intent):
    course = _save_course(intent)
    assert course["group_id"] == "g1" and course["maintainer"] == "teacher"
    assert course["status"] == "confirmed" and course["location"] == "93501"
    assert not db.list_training_sessions("g2", active_only=False)
    if intent == teacher_intent.INTENT_ONE_OFF_SESSION:
        assert course["effective_start"] == "2026-11-19 00:00"
        assert course["effective_end"] == "2026-11-19 23:59"


@pytest.mark.parametrize("change", ["owner", "whitelist", "group"])
def test_new_course_rechecks_permission_at_write_transaction_start(env, monkeypatch, change):
    db.set_group_teachers("g1", ["teacher"], operator="admin")
    assert teacher_intent.is_teacher_speaker("g1", "teacher")
    original = teacher_intent._course_info
    after_race = None

    def info_with_race(*args):
        nonlocal after_race
        info = original(*args)
        if change == "owner":
            db.set_group_teachers("g1", ["other"], operator="admin")
        elif change == "whitelist":
            monkeypatch.setattr(config, "TEACHER_WHITELIST", {"other"})
        else:
            monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g2"})
        after_race = _business_snapshot()
        return info

    monkeypatch.setattr(teacher_intent, "_course_info", info_with_race)
    assert _save_course() is None
    assert _business_snapshot() == after_race


def test_new_course_failed_insert_rolls_back_without_sequence_or_record(env):
    with closing(db._connect()) as conn, conn:
        conn.execute("CREATE TRIGGER offline_reject BEFORE INSERT ON training_sessions"
                     " BEGIN SELECT RAISE(ABORT, 'offline failure'); END")
    before = _business_snapshot()
    with pytest.raises(sqlite3.IntegrityError, match="offline failure"):
        _save_course()
    assert _business_snapshot() == before


def test_new_course_failed_fresh_read_rolls_back_created_record(env, monkeypatch):
    monkeypatch.setattr(business_followup, "_record", lambda *args: None)
    before = _business_snapshot()
    assert _save_course() is None
    assert _business_snapshot() == before


def test_create_training_borrowed_connection_does_not_commit(env):
    before = _business_snapshot()
    with closing(db._connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        tid = db.create_training_session("g1", {"title": "未提交课程", "weekday": "周四", "start_time": "19:00"}, conn=conn)
        assert tid and conn.in_transaction
        assert _business_snapshot() == before
        conn.rollback()
    assert _business_snapshot() == before


@pytest.mark.parametrize("intent,confidence", [
    (teacher_intent.INTENT_RECURRING_COURSE, "high"),
    (teacher_intent.INTENT_RECURRING_COURSE, "medium"),
    (teacher_intent.INTENT_POLICY_FAQ, "high"),
])
@pytest.mark.parametrize("change", ["owner", "whitelist", "group"])
async def test_permission_revoked_while_classifying_does_not_create_business_pending_or_faq(env, monkeypatch, intent, confidence, change):
    db.set_group_teachers("g1", ["teacher"], operator="admin")
    monkeypatch.setattr(config, "TEACHER_AUTO_INTENT_ENABLED", True)
    policy = AsyncMock()
    monkeypatch.setattr(teacher_intent, "_apply_policy_faq", policy)
    after_race = None

    async def classify_with_race(*args, **kwargs):
        nonlocal after_race
        if change == "owner":
            db.set_group_teachers("g1", ["other"], operator="admin")
        elif change == "whitelist":
            monkeypatch.setattr(config, "TEACHER_WHITELIST", {"other"})
        else:
            monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g2"})
        after_race = _business_snapshot()
        return {"intent": intent, "confidence": confidence, "title": "新集训", "weekday": "周四", "start_time": "19:00"}

    monkeypatch.setattr(teacher_intent, "classify", classify_with_race)
    result = await teacher_intent.handle_message("每周四晚上7点集训", group_id="g1", sender_id="teacher", now=NOW)
    assert result["action"] == "ignored" and not result["reply"]
    policy.assert_not_awaited()
    assert _business_snapshot() == after_race
