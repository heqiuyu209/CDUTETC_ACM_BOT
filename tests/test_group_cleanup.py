"""群清理以隔离 SQLite 验证范围、预览快照、备份与提醒一致性。"""

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from cdutetc import config, database as db, group_cleanup as cleanup
from scripts import reset_business_data as reset

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=TZ)
OLD = "2026-09-01 19:00"
FUTURE = "2026-12-20 19:00"


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "cleanup.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    monkeypatch.setattr(db, "_delivery_now", lambda value=None: value or NOW)
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "WEEKLY_CONTEST_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "CONTEST_REMIND_OFFSETS_MINUTES", (30, 15))
    db.init_db()
    return path


def _query(path, sql, parameters=()):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(sql, parameters).fetchall()


def _execute(path, sql, parameters=()):
    with closing(sqlite3.connect(path)) as conn:
        cursor = conn.execute(sql, parameters)
        conn.commit()
        return cursor.lastrowid


def _contest(title="正式校赛", when=OLD, groups=None, deadline=""):
    cid = db.create_competition({"name": title, "official_time": when,
                                 "official_reg_deadline": deadline}, status="confirmed")
    if groups is not None:
        db.set_competition_notify_groups(cid, groups)
    return cid


def _preview(mode="test_data", group="g1", days=0, operator="admin", now=NOW):
    return cleanup.preview_cleanup(group, mode, days=days, operator=operator, now=now)


def _apply(preview, group="g1", operator="admin", now=NOW):
    return cleanup.apply_cleanup(group, preview["token"], operator=operator, now=now)


def _ids(preview):
    return {(item["type"], item["id"]) for item in preview["items"]}


def _protected(path):
    result = {table: _query(path, f'SELECT * FROM "{table}"') for table in reset.CONFIG_TABLES}
    # 新审计条目可推进自身编号；业务编号不应因清理而回退或重用。
    result["sqlite_sequence"] = _query(path, "SELECT * FROM sqlite_sequence WHERE name!='audit_log'")
    return result


def test_expired_preview_does_not_change_business_records_or_create_backup(env):
    cid = _contest()
    before = _query(env, "SELECT * FROM competitions")
    protected = _protected(env)
    result = _preview("expired_contests", days=30)
    assert _ids(result) == {("competition", cid)}
    assert result["token"]
    assert _query(env, "SELECT * FROM competitions") == before
    assert _protected(env) == protected
    assert not list(env.parent.rglob("*before-reset*.sqlite3"))


@pytest.mark.parametrize("when,deadline,expected", [
    (OLD, "", True),
    (OLD, "2026-09-02 23:59", True),
    ("", OLD, False),
    ("bad-time", "", False),
    (OLD, "invalid-deadline", False),
    (OLD, FUTURE, False),
    (FUTURE, OLD, False),
    ("2026-10-19 12:00", "", False),
    ("2026-10-19 11:59", "", True),
])
def test_expiry_requires_past_start_and_all_dates_parse_before_cutoff(env, when, deadline, expected):
    cid = _contest()
    _execute(env, "UPDATE competitions SET official_time=?, official_reg_deadline=? WHERE id=?",
             (when, deadline, cid))
    assert (("competition", cid) in _ids(_preview("expired_contests", days=30))) is expected


@pytest.mark.parametrize("groups", [None, ["g1"], ["g1", "g2"]])
def test_cleanup_archives_only_current_group_without_changing_shared_scope(env, groups):
    cid = _contest(groups=groups)
    original_targets = db.get_competition_target_groups(cid)
    original_explicit = db.get_competition_notify_groups(cid)
    original = db.get_competition(cid)
    result = _apply(_preview("expired_contests", days=30))
    assert result["cleared"].get("competition") == 1
    assert db.get_competition(cid) == original
    assert db.get_competition_notify_groups(cid) == original_explicit
    assert db.get_competition_target_groups(cid) == original_targets - {"g1"}
    assert cid not in {item["id"] for item in db.list_group_competitions("g1")}
    assert not any(item["name"] == original["name"] for item in db.list_question_contests("g1"))
    if "g2" in original_targets:
        assert cid in {item["id"] for item in db.list_group_competitions("g2")}
        assert any(item["name"] == original["name"] for item in db.list_question_contests("g2"))


def test_archive_cancels_only_group_nodes_and_refresh_does_not_recreate_them(env):
    cid = _contest(when=FUTURE)
    before_other = _query(env, "SELECT * FROM reminder_deliveries WHERE group_id='g2'")
    assert before_other
    cleanup.mark_test("g1", "competition", cid, operator="admin", now=NOW)
    _apply(_preview())
    assert _query(env, "SELECT status, lease_token, lease_until FROM reminder_deliveries WHERE group_id='g1'")
    assert all(row == ("cancelled", "", "") for row in _query(
        env, "SELECT status, lease_token, lease_until FROM reminder_deliveries WHERE group_id='g1'"))
    assert _query(env, "SELECT * FROM reminder_deliveries WHERE group_id='g2'") == before_other
    db.sync_future_contest_start_reminders(now=NOW)
    db.init_db()
    assert "g1" not in db.get_competition_target_groups(cid)
    assert not _query(env, "SELECT 1 FROM reminder_deliveries WHERE group_id='g1' AND status IN ('pending','sending')")


def test_restore_competition_reenables_only_archived_group_and_is_idempotent(env):
    cid = _contest(when=FUTURE)
    cleanup.mark_test("g1", "competition", cid, operator="admin", now=NOW)
    _apply(_preview())
    assert cleanup.restore_competition("g1", cid, operator="admin")
    assert db.get_competition_target_groups(cid) == {"g1", "g2"}
    assert not cleanup.restore_competition("g1", cid, operator="admin")
    db.sync_future_contest_start_reminders(now=NOW)
    assert _query(env, "SELECT 1 FROM reminder_deliveries WHERE group_id='g1' AND status='pending'")


def test_restore_future_nodes_preserves_non_exhausted_retry_attempts(env):
    cid = _contest(when=FUTURE)
    _execute(env, "UPDATE reminder_deliveries SET attempts=1,last_error='temporary_failure'"
                  " WHERE group_id='g1' AND obj_id=?", (cid,))
    other = _query(env, "SELECT * FROM reminder_deliveries WHERE group_id='g2'")
    cleanup.mark_test("g1", "competition", cid, operator="admin", now=NOW)
    _apply(_preview())
    assert _query(env, "SELECT status,attempts FROM reminder_deliveries WHERE group_id='g1'") == [
        ("cancelled", 1), ("cancelled", 1),
    ]
    assert cleanup.restore_competition("g1", cid, operator="admin")
    assert _query(env, "SELECT status,attempts FROM reminder_deliveries WHERE group_id='g1'") == [
        ("pending", 1), ("pending", 1),
    ]
    assert _query(env, "SELECT * FROM reminder_deliveries WHERE group_id='g2'") == other


@pytest.mark.parametrize("status,attempts,expected", [
    ("pending", 3, "cancelled"), ("failed", 3, "failed"),
    ("sent", 1, "sent"), ("expired", 1, "expired"),
])
def test_restore_keeps_exhausted_successful_and_expired_delivery_history(env, status, attempts, expected):
    cid = _contest(when=FUTURE)
    _execute(env, "UPDATE reminder_deliveries SET status=?,attempts=? WHERE group_id='g1' AND obj_id=?",
             (status, attempts, cid))
    cleanup.mark_test("g1", "competition", cid, operator="admin", now=NOW)
    _apply(_preview())
    assert cleanup.restore_competition("g1", cid, operator="admin")
    assert _query(env, "SELECT status,attempts FROM reminder_deliveries WHERE group_id='g1'") == [
        (expected, attempts), (expected, attempts),
    ]


def test_shared_contest_metadata_sources_claims_and_weekly_anchor_are_preserved(env):
    cid, _ = db.create_weekly_competition("2026-08-31", {"name": "旧周赛", "official_time": OLD})
    db.add_competition_source(cid, "https://example.invalid/weekly")
    _execute(env, "INSERT INTO publication_claims(identity_key,competition_id) VALUES (?,?)", ("old-claim", cid))
    tables = ("competitions", "competition_sources", "competition_versions",
              "competition_notify_groups", "weekly_contest_runs", "publication_claims")
    before = {table: _query(env, f'SELECT * FROM "{table}"') for table in tables}
    _apply(_preview("expired_contests", days=30))
    assert {table: _query(env, f'SELECT * FROM "{table}"') for table in tables} == before
    repeated, created = db.create_weekly_competition("2026-08-31", {"name": "旧周赛", "official_time": OLD})
    assert repeated == cid and not created


def test_test_mode_uses_explicit_marks_not_names_or_small_ids(env):
    unmarked = _contest(title="测试比赛 demo", when=FUTURE)
    marked = _contest(title="正式校赛", when=FUTURE)
    cleanup.mark_test("g1", "competition", marked, operator="admin", now=NOW)
    assert _ids(_preview()) == {("competition", marked)}
    _apply(_preview())
    assert unmarked in {item["id"] for item in db.list_group_competitions("g1")}
    assert marked in {item["id"] for item in db.list_group_competitions("g2")}


def _seed(group, object_type):
    if object_type == "competition":
        return _contest(when=FUTURE, groups=[group])
    if object_type == "legacy_contest":
        with closing(db._connect()) as conn, conn:
            return conn.execute("INSERT INTO contests(group_id,name,contest_time) VALUES (?,?,?)",
                                (group, "旧链比赛", FUTURE)).lastrowid
    if object_type == "collection":
        return db.create_collection(group, "训练登记", deadline=FUTURE)
    if object_type == "training_session":
        return db.create_training_session(group, {"title": "训练课", "weekday": "周三", "start_time": "19:00"})
    if object_type == "custom_reminder":
        return db.create_custom_reminder(group, "交周报", FUTURE)
    if object_type == "question":
        return db.add_unanswered_question(group, "如何报名？", "member")
    if object_type == "faq":
        return db.add_faq(group, "如何报名？", "找老师")
    if object_type == "publication":
        return db.create_pending_publication({"name": "待审校赛", "official_time": FUTURE},
                                             group, "", "", "teacher")
    raise AssertionError(object_type)


TABLES = {"legacy_contest": "contests", "collection": "collections", "training_session": "training_sessions",
          "custom_reminder": "custom_reminders", "question": "unanswered_questions", "faq": "group_faq",
          "publication": "pending_publications"}


@pytest.mark.parametrize("object_type", ["competition", *TABLES])
def test_marked_types_are_cleaned_only_in_their_own_group(env, object_type):
    current = _seed("g1", object_type)
    other = _seed("g2", object_type)
    cleanup.mark_test("g1", object_type, current, operator="admin", now=NOW)
    cleanup.mark_test("g2", object_type, other, operator="admin", now=NOW)
    preview = _preview()
    assert _ids(preview) == {(object_type, current)}
    result = _apply(preview)
    assert result["cleared"].get(object_type) == 1
    if object_type == "competition":
        assert current not in {item["id"] for item in db.list_group_competitions("g1")}
        assert other in {item["id"] for item in db.list_group_competitions("g2")}
    else:
        assert not _query(env, f'SELECT 1 FROM "{TABLES[object_type]}" WHERE id=?', (current,))
        assert _query(env, f'SELECT 1 FROM "{TABLES[object_type]}" WHERE id=?', (other,))
        assert isinstance(result["backup"], Path) and result["backup"].is_file()


@pytest.mark.parametrize("object_type", ["competition", *TABLES])
def test_cross_group_test_mark_is_rejected(env, object_type):
    foreign = _seed("g2", object_type)
    with pytest.raises(ValueError):
        cleanup.mark_test("g1", object_type, foreign, operator="admin", now=NOW)
    assert not _preview()["items"]


def test_unmark_test_removes_candidate(env):
    cid = _contest(when=FUTURE)
    cleanup.mark_test("g1", "competition", cid, operator="admin", now=NOW)
    cleanup.unmark_test("g1", "competition", cid, operator="admin", now=NOW)
    assert not _preview()["items"]


def test_prepared_token_rejects_wrong_user_group_expiry_and_reuse(env):
    cid = _contest()
    preview = _preview("expired_contests", days=30)
    for group, user, now in (("g2", "admin", NOW), ("g1", "other-admin", NOW),
                              ("g1", "admin", NOW + timedelta(minutes=11))):
        with pytest.raises(ValueError):
            _apply(preview, group=group, operator=user, now=now)
        assert "g1" in db.get_competition_target_groups(cid)
    valid = _preview("expired_contests", days=30)
    _apply(valid)
    with pytest.raises(ValueError):
        _apply(valid)


def test_preview_snapshot_does_not_expand_to_new_records_or_changed_records(env):
    stable = _contest(title="稳定记录")
    changed = _contest(title="后来改期")
    preview = _preview("expired_contests", days=30)
    added = _contest(title="预览后新增")
    _execute(env, "UPDATE competitions SET official_time=? WHERE id=?", (FUTURE, changed))
    result = _apply(preview)
    assert result["cleared"].get("competition") == 1
    assert result["skipped"] >= 1
    targets = {cid: db.get_competition_target_groups(cid) for cid in (stable, changed, added)}
    assert targets[stable] == {"g2"}
    assert targets[changed] == targets[added] == {"g1", "g2"}


def test_notification_scope_change_invalidates_competition_preview(env):
    cid = _contest(groups=["g1"])
    preview = _preview("expired_contests", days=30)
    db.set_competition_notify_groups(cid, ["g1", "g2"])
    result = _apply(preview)
    assert not result["cleared"].get("competition", 0)
    assert result["skipped"] >= 1
    assert db.get_competition_target_groups(cid) == {"g1", "g2"}


def test_collection_responses_changed_after_preview_are_not_deleted(env):
    coll = db.create_collection("g1", "报名", deadline=FUTURE)
    cleanup.mark_test("g1", "collection", coll, operator="admin", now=NOW)
    preview = _preview()
    assert db.register_collection(coll, "g1", "student", {})[0]
    result = _apply(preview)
    assert not result["cleared"].get("collection", 0)
    assert result["skipped"] >= 1
    assert db.get_collection(coll) is not None
    assert _query(env, "SELECT 1 FROM collection_responses WHERE collection_id=?", (coll,))


def test_collection_delete_removes_its_responses_and_preserves_other_group(env):
    current = db.create_collection("g1", "测试登记", deadline=FUTURE)
    other = db.create_collection("g2", "正式登记", deadline=FUTURE)
    assert db.register_collection(current, "g1", "s1", {})[0]
    assert db.register_collection(other, "g2", "s2", {})[0]
    cleanup.mark_test("g1", "collection", current, operator="admin", now=NOW)
    result = _apply(_preview())
    assert result["cleared"].get("collection") == 1
    assert not _query(env, "SELECT 1 FROM collection_responses WHERE collection_id=?", (current,))
    assert _query(env, "SELECT 1 FROM collection_responses WHERE collection_id=?", (other,))


def test_cleaning_test_faq_reopens_linked_local_question(env):
    question = db.add_unanswered_question("g1", "如何报名？", "s1")
    faq = db.add_faq("g1", "如何报名？", "测试答案")
    other = db.add_unanswered_question("g2", "如何报名？", "s2")
    assert _query(env, "SELECT status,faq_id FROM unanswered_questions WHERE id=?", (question,)) == [("answered", faq)]
    cleanup.mark_test("g1", "faq", faq, operator="admin", now=NOW)
    _apply(_preview())
    assert _query(env, "SELECT status,faq_id FROM unanswered_questions WHERE id=?", (question,)) == [("open", 0)]
    assert _query(env, "SELECT status,faq_id FROM unanswered_questions WHERE id=?", (other,)) == [("open", 0)]


def test_faq_with_corrupt_cross_group_reference_is_skipped(env):
    faq = db.add_faq("g1", "如何报名？", "测试答案")
    foreign = db.add_unanswered_question("g2", "如何报名？", "s2")
    _execute(env, "UPDATE unanswered_questions SET status='answered',faq_id=? WHERE id=?", (faq, foreign))
    cleanup.mark_test("g1", "faq", faq, operator="admin", now=NOW)
    preview = _preview()
    assert not preview["items"] and preview["skipped"] >= 1
    assert db.get_faq("g1", faq) is not None
    assert _query(env, "SELECT status,faq_id FROM unanswered_questions WHERE id=?", (foreign,)) == [("answered", faq)]


def test_sending_delivery_is_skipped_without_cancelling_live_claim(env):
    cid = _contest(when=FUTURE)
    cleanup.mark_test("g1", "competition", cid, operator="admin", now=NOW)
    _execute(env, "UPDATE reminder_deliveries SET status='sending',lease_token='active',lease_until=?"
             " WHERE group_id='g1' AND obj_id=?", ((NOW + timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S"), cid))
    preview = _preview()
    if preview["items"]:
        result = _apply(preview)
        assert not result["cleared"].get("competition", 0)
        assert result["skipped"] >= 1
    else:
        assert preview["skipped"] >= 1
    assert "g1" in db.get_competition_target_groups(cid)
    assert _query(env, "SELECT lease_token FROM reminder_deliveries WHERE group_id='g1' AND status='sending'") == [("active",), ("active",)]


def test_delivery_claim_started_after_preview_blocks_cleanup(env):
    cid = _contest(when=FUTURE)
    cleanup.mark_test("g1", "competition", cid, operator="admin", now=NOW)
    preview = _preview()
    _execute(env, "UPDATE reminder_deliveries SET status='sending',lease_token='new-active',lease_until=?"
             " WHERE group_id='g1' AND obj_id=?", ((NOW + timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S"), cid))
    result = _apply(preview)
    assert not result["cleared"].get("competition", 0)
    assert result["skipped"] >= 1
    assert "g1" in db.get_competition_target_groups(cid)


def test_restore_archive_preserves_other_groups_live_delivery_claim(env):
    cid = _contest(when=FUTURE)
    cleanup.mark_test("g1", "competition", cid, operator="admin", now=NOW)
    _apply(_preview())
    _execute(env, "UPDATE reminder_deliveries SET status='sending',lease_token='other-live',lease_until=?"
             " WHERE group_id='g2' AND obj_id=?", ((NOW + timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S"), cid))
    before = _query(env, "SELECT * FROM reminder_deliveries WHERE group_id='g2'")
    assert cleanup.restore_competition("g1", cid, operator="admin")
    assert _query(env, "SELECT * FROM reminder_deliveries WHERE group_id='g2'") == before


def test_backup_failure_keeps_records_and_confirmation_retryable(env, monkeypatch):
    coll = db.create_collection("g1", "测试登记", deadline=FUTURE)
    cleanup.mark_test("g1", "collection", coll, operator="admin", now=NOW)
    preview = _preview()
    def fail(*_args, **_kwargs):
        raise OSError("backup unavailable")
    with monkeypatch.context() as patch:
        patch.setattr(cleanup, "_backup", fail)
        with pytest.raises(OSError, match="backup unavailable"):
            _apply(preview)
    assert db.get_collection(coll) is not None
    with closing(sqlite3.connect(env, timeout=0.1)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()
    assert _apply(preview)["cleared"].get("collection") == 1


def test_failed_delete_rolls_back_earlier_records(env):
    first = db.create_collection("g1", "测试一", deadline=FUTURE)
    second = db.create_collection("g1", "测试二", deadline=FUTURE)
    for coll in (first, second):
        cleanup.mark_test("g1", "collection", coll, operator="admin", now=NOW)
    preview = _preview()
    _execute(env, f"CREATE TRIGGER refuse_delete BEFORE DELETE ON collections WHEN OLD.id={second}"
             " BEGIN SELECT RAISE(ABORT,'refused'); END")
    with pytest.raises(sqlite3.IntegrityError, match="refused"):
        _apply(preview)
    assert db.get_collection(first) is not None and db.get_collection(second) is not None


def test_physical_cleanup_preserves_configuration_success_receipts_and_ids(env):
    coll = db.create_collection("g1", "测试报名", deadline=FUTURE)
    db.set_group_setting("g1", "qa_enabled", "off", "admin")
    db.set_admin_workspace("admin", "g1")
    db.confirm_group_notification_channel("qqbot", "g1", "markdown")
    _execute(env, "INSERT INTO reminder_success_receipts(dedup_key,lease_token,sent_at) VALUES (?,?,?)",
             ("g1|collection|100|deadline|2026-01-01", "sent-token", "2026-01-01 10:00:00"))
    before = _protected(env)
    receipts = _query(env, "SELECT * FROM reminder_success_receipts")
    cleanup.mark_test("g1", "collection", coll, operator="admin", now=NOW)
    result = _apply(_preview())
    assert _protected(env) == before
    assert _query(env, "SELECT * FROM reminder_success_receipts") == receipts
    assert _query(result["backup"], "SELECT 1 FROM collections WHERE id=?", (coll,))
    assert db.create_collection("g1", "正式新登记") > coll


def test_rules_default_disabled_and_manual_marker_cleanup_does_not_enable_them(env):
    rules = cleanup.get_rules("g1")
    assert rules and all(not rule["enabled"] for rule in rules.values())
    assert not cleanup.run_rules(now=NOW)


def test_rules_persist_and_only_enabled_group_is_cleaned(env):
    cid = _contest()
    cleanup.set_rule("g1", "expired_contests", 30, True, operator="admin")
    cleanup.set_rule("g2", "expired_contests", 30, False, operator="admin")
    db.init_db()
    rule = cleanup.get_rules("g1")["expired_contests"]
    assert rule["days"] == 30 and rule["enabled"] is True
    assert not cleanup.get_rules("g2")["expired_contests"]["enabled"]
    results = cleanup.run_rules(now=NOW)
    assert "g1" in results and "g2" not in results
    assert db.get_competition_target_groups(cid) == {"g2"}


def test_test_rule_respects_marker_age_and_disabled_groups(env):
    old = _contest(title="旧标记", when=FUTURE)
    recent = _contest(title="刚标记", when=FUTURE)
    cleanup.mark_test("g1", "competition", old, operator="admin", now=NOW - timedelta(days=8))
    cleanup.mark_test("g2", "competition", old, operator="admin", now=NOW - timedelta(days=8))
    cleanup.mark_test("g1", "competition", recent, operator="admin", now=NOW)
    cleanup.set_rule("g1", "test_data", 7, True, operator="admin")
    results = cleanup.run_rules(now=NOW)
    assert results["g1"]["cleared"].get("competition") == 1
    assert db.get_competition_target_groups(old) == {"g2"}
    assert db.get_competition_target_groups(recent) == {"g1", "g2"}


def test_rule_runs_at_most_once_per_day_and_new_candidates_wait_until_next_day(env):
    first = _contest(title="第一条过期记录")
    cleanup.set_rule("g1", "expired_contests", 30, True, operator="admin")
    cleanup.run_rules(now=NOW)
    second = _contest(title="当日运行后新记录")
    assert not cleanup.run_rules(now=NOW + timedelta(hours=1))
    assert db.get_competition_target_groups(second) == {"g1", "g2"}
    assert db.get_competition_target_groups(first) == {"g2"}
    assert cleanup.run_rules(now=NOW + timedelta(days=1))["g1"]["cleared"].get("competition") == 1
    assert db.get_competition_target_groups(second) == {"g2"}


def test_one_group_rule_failure_does_not_stop_other_group_and_is_retryable(env):
    first = _contest(title="第一群旧记录", groups=["g1"])
    second = _contest(title="第二群旧记录", groups=["g2"])
    for gid in ("g1", "g2"):
        cleanup.set_rule(gid, "expired_contests", 30, True, operator="admin")
    _execute(env, "CREATE TRIGGER fail_first_group BEFORE INSERT ON group_competition_archives"
             " WHEN NEW.group_id='g1' BEGIN SELECT RAISE(ABORT,'refused'); END")
    results = cleanup.run_rules(now=NOW)
    assert results["g1"]["errors"]
    assert results["g2"]["cleared"].get("competition") == 1
    assert db.get_competition_target_groups(first) == {"g1"}
    assert db.get_competition_target_groups(second) == set()
    assert not cleanup.get_rules("g1")["expired_contests"]["last_run"]
    _execute(env, "DROP TRIGGER fail_first_group")
    assert cleanup.run_rules(now=NOW)["g1"]["cleared"].get("competition") == 1


def test_backup_then_delete_transaction_blocks_concurrent_claim_creation(env, monkeypatch):
    cid = _contest(when=FUTURE)
    cleanup.mark_test("g1", "competition", cid, operator="admin", now=NOW)
    real_backup = cleanup._backup
    def probe(path, directory):
        with closing(sqlite3.connect(env, timeout=0.01)) as competitor:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competitor.execute("BEGIN IMMEDIATE")
        return real_backup(path, directory)
    monkeypatch.setattr(cleanup, "_backup", probe)
    assert _apply(_preview())["cleared"].get("competition") == 1


@pytest.mark.parametrize("days", [0, -1, True, "30"])
def test_rules_reject_invalid_retention(env, days):
    with pytest.raises(ValueError):
        cleanup.set_rule("g1", "test_data", days, True, operator="admin")


def test_legacy_expired_cleanup_is_physically_group_scoped(env):
    first = _seed("g1", "legacy_contest")
    second = _seed("g2", "legacy_contest")
    _execute(env, "UPDATE contests SET contest_time=?", (OLD,))
    preview = _preview("expired_contests", days=30)
    assert _ids(preview) == {("legacy_contest", first)}
    result = _apply(preview)
    assert result["cleared"].get("legacy_contest") == 1
    assert not _query(env, "SELECT 1 FROM contests WHERE id=?", (first,))
    assert _query(env, "SELECT 1 FROM contests WHERE id=?", (second,))
