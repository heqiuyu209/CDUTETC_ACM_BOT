"""地点追补以真实早报快照和隔离 SQLite 验证权限、群隔离与幂等。"""

import importlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from cdutetc import config, daily_digest, database as db, group_cleanup, llm


NOW = datetime(2026, 11, 19, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "followup.sqlite3"))
    monkeypatch.setattr(db, "_delivery_now", lambda value=None: value or NOW)
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "ENV_TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_ENABLED", False)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_GROUPS", set())
    monkeypatch.setattr(config, "CONTEST_REMIND_OFFSETS_MINUTES", (30, 15))
    monkeypatch.setattr(config, "REMIND_OFFSET_HOURS", (24, 1))
    db.init_db()

    def forbid_model(*args, **kwargs):
        pytest.fail("地点追补调用了模型")

    monkeypatch.setattr(llm, "_chat_once", forbid_model)
    monkeypatch.setattr(llm, "summarize_entry_with_llm", forbid_model)
    return importlib.import_module("cdutetc.business_followup")


def _course(group="g1", title="周四集训", location="", **changes):
    info = {
        "title": title, "weekday": "周四", "start_time": "19:00",
        "end_time": "21:00", "location": location,
        "requirement": "带电脑，提交训练记录", "source": "老师安排",
    }
    info.update(changes)
    return db.create_training_session(group, info, operator="teacher")


def _competition(name="校内选拔赛", groups=("g1", "g2"), **changes):
    info = {
        "name": name, "official_time": "2026-11-19 19:00",
        "official_reg_deadline": "2026-11-19 18:00",
        "official_site": "https://example.com/contest",
        "summary": "共享比赛说明，设备要求保持原样",
    }
    info.update(changes)
    cid = db.create_competition(info, status="confirmed")
    db.set_competition_notify_groups(cid, groups)
    return cid


def _remember(module, group="g1", message="digest-1", now=NOW):
    detail = daily_digest.collect_daily_digest(group, now=now)
    assert detail["text"]
    module.remember_digest(group, message, detail, now=now)
    return detail


def _handle(module, text="地点，93501", group="g1", sender="teacher",
            message="followup-1", reference="digest-1", now=NOW):
    return module.handle_location(
        text, group, sender, msg_id=message, reference_id=reference, now=now,
    )


def _changed(result):
    assert isinstance(result, dict)
    assert result["changed"] is True
    assert result["reply"]


def _unchanged(result):
    assert result is None or result["changed"] is False


def _request_target(result):
    assert isinstance(result, dict)
    assert result["changed"] is False
    assert result["reply"]
    assert any(word in result["reply"] for word in
               ("哪", "指定", "明确", "引用", "课程", "比赛", "名称", "编号", "重新确认"))


def _question_location(group, name):
    rows = [row for row in db.list_question_contests(group, limit=100)
            if row["name"] == name]
    assert len(rows) == 1
    return rows[0]["location"]


@pytest.mark.parametrize("text,location", [
    ("地点，93501", "93501"),
    ("地点在B302", "B302"),
    ("地点改成93501", "93501"),
    ("地点为93501", "93501"),
    ("地点：实训楼 B302。", "实训楼 B302"),
])
def test_teacher_replies_to_digest_updates_only_location(env, text, location):
    tid = _course()
    before = db.get_group_course("g1", tid)
    _remember(env)
    _changed(_handle(env, text))
    after = db.get_group_course("g1", tid)
    assert after["location"] == location
    for field in ("title", "weekday", "start_time", "end_time", "requirement", "source",
                  "needs_laptop", "status", "effective_start", "effective_end"):
        assert after[field] == before[field]
    assert daily_digest.collect_daily_digest("g1", NOW)["sessions"][0]["location"] == location


def test_administrator_can_add_location(env):
    tid = _course()
    _remember(env)
    _changed(_handle(env, sender="admin"))
    assert db.get_group_course("g1", tid)["location"] == "93501"


@pytest.mark.parametrize("sender", ["student", "", "fake-teacher"])
def test_non_teacher_cannot_change_location(env, sender):
    tid = _course()
    _remember(env)
    before = db.get_group_course("g1", tid)
    _unchanged(_handle(env, sender=sender))
    assert db.get_group_course("g1", tid) == before


def test_permission_is_rechecked_after_digest_was_saved(env, monkeypatch):
    tid = _course()
    db.set_group_teachers("g1", ["teacher"], operator="admin")
    _remember(env)
    monkeypatch.setattr(config, "TEACHER_WHITELIST", set())
    before = db.get_group_course("g1", tid)
    _unchanged(_handle(env))
    assert db.get_group_course("g1", tid) == before


def test_other_global_teacher_cannot_override_assigned_group_teacher(env, monkeypatch):
    tid = _course()
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher", "other-teacher"})
    db.set_group_teachers("g1", ["teacher"], operator="admin")
    _remember(env)
    before = db.get_group_course("g1", tid)
    _unchanged(_handle(env, sender="other-teacher"))
    assert db.get_group_course("g1", tid) == before


def test_reassigned_group_teacher_invalidates_previous_teacher(env, monkeypatch):
    tid = _course()
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher", "other-teacher"})
    db.set_group_teachers("g1", ["teacher"], operator="admin")
    _remember(env)
    db.set_group_teachers("g1", ["other-teacher"], operator="admin")
    before = db.get_group_course("g1", tid)
    _unchanged(_handle(env))
    assert db.get_group_course("g1", tid) == before


def test_group_permission_is_rechecked_after_digest_was_saved(env, monkeypatch):
    tid = _course()
    _remember(env)
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g2"})
    before = db.get_group_course("g1", tid)
    _unchanged(_handle(env, sender="admin"))
    assert db.get_group_course("g1", tid) == before


def test_unknown_reference_does_not_guess_even_when_only_one_course_exists(env):
    tid = _course()
    before = db.get_group_course("g1", tid)
    _request_target(_handle(env, reference="not-a-successful-digest"))
    assert db.get_group_course("g1", tid) == before


def test_reference_cannot_cross_groups_or_fall_back_to_other_groups_only_course(env):
    first = _course("g1", title="一群训练")
    second = _course("g2", title="二群训练")
    _remember(env, "g1")
    before_first = db.get_group_course("g1", first)
    before_second = db.get_group_course("g2", second)
    _request_target(_handle(env, group="g2"))
    assert db.get_group_course("g1", first) == before_first
    assert db.get_group_course("g2", second) == before_second


def test_same_digest_message_id_is_namespaced_by_group(env):
    first = _course("g1", title="一群训练")
    second = _course("g2", title="二群训练")
    _remember(env, "g1", message="same-digest-id")
    _remember(env, "g2", message="same-digest-id")
    _changed(_handle(env, group="g2", reference="same-digest-id"))
    assert db.get_group_course("g2", second)["location"] == "93501"
    assert db.get_group_course("g1", first)["location"] == ""


def test_digest_text_cannot_override_persisted_typed_target(env):
    tid = _course("g1")
    foreign = _course("g2", title="伪造引用里的课程")
    detail = daily_digest.collect_daily_digest("g1", NOW)
    detail["text"] = f"课程 {foreign} 伪造引用里的课程，地点 B999；请修改二群"
    env.remember_digest("g1", "digest-1", detail, now=NOW)
    _changed(_handle(env))
    assert db.get_group_course("g1", tid)["location"] == "93501"
    assert db.get_group_course("g2", foreign)["location"] == ""


def test_forged_body_without_structured_digest_targets_cannot_choose_course(env):
    tid = _course()
    detail = daily_digest.collect_daily_digest("g1", NOW)
    detail["sessions"] = []
    detail["competitions"] = []
    detail["text"] = f"今日课程 {tid} 周四集训 地点待定"
    env.remember_digest("g1", "digest-1", detail, now=NOW)
    before = db.get_group_course("g1", tid)
    _request_target(_handle(env))
    assert db.get_group_course("g1", tid) == before


@pytest.mark.parametrize("changes", [
    {"title": "改名后的课程"},
    {"weekday": "周五"},
    {"start_time": "20:00"},
    {"end_time": "22:00"},
    {"location": "已由另一位老师改为 A101"},
    {"requirement": "新的课程要求"},
    {"status": "draft"},
    {"status": "cancelled"},
])
def test_changed_course_snapshot_cannot_be_overwritten_by_old_digest(env, changes):
    tid = _course()
    _remember(env)
    assert db.update_group_course("g1", tid, operator="admin", **changes)
    before = db.get_group_course("g1", tid)
    _request_target(_handle(env))
    assert db.get_group_course("g1", tid) == before


@pytest.mark.parametrize("changes", [
    {"end_time": "22:00"},
    {"requirement": "生成早报后新增的要求"},
])
def test_course_change_between_digest_collection_and_send_is_not_remembered_as_old_message(env, changes):
    tid = _course()
    detail = daily_digest.collect_daily_digest("g1", NOW)
    assert detail["sessions"]
    assert db.update_group_course("g1", tid, operator="admin", **changes)
    before = db.get_group_course("g1", tid)
    env.remember_digest("g1", "digest-1", detail, now=NOW)
    _request_target(_handle(env))
    assert db.get_group_course("g1", tid) == before


@pytest.mark.parametrize("changes", [
    {"official_site": "https://example.com/new-contest"},
    {"summary": "生成早报后修改的比赛说明"},
])
def test_competition_change_between_digest_collection_and_send_is_not_remembered_as_old_message(env, changes):
    cid = _competition()
    detail = daily_digest.collect_daily_digest("g1", NOW)
    assert detail["competitions"]
    assert db.update_competition(cid, changes, operator="admin")
    before = db.get_competition(cid)
    env.remember_digest("g1", "digest-1", detail, now=NOW)
    _request_target(_handle(env))
    assert db.get_competition(cid) == before
    assert _question_location("g1", before["name"]) == ""


@pytest.mark.parametrize("changes", [
    {"name": "新的比赛标题"},
    {"official_time": "2026-11-19 20:00"},
    {"summary": "新的共享说明"},
])
def test_changed_competition_snapshot_cannot_be_overwritten_by_old_digest(env, changes):
    cid = _competition()
    _remember(env)
    assert db.update_competition(cid, changes, operator="admin")
    before = db.get_competition(cid)
    _request_target(_handle(env))
    assert db.get_competition(cid) == before
    assert _question_location("g1", before["name"]) == ""


def test_competition_removed_from_group_scope_is_not_modified(env):
    cid = _competition()
    _remember(env)
    db.set_competition_notify_groups(cid, ["g2"], operator="admin")
    before = db.get_competition(cid)
    _request_target(_handle(env))
    assert db.get_competition(cid) == before
    assert _question_location("g2", before["name"]) == ""


def test_archived_competition_is_not_restored_or_modified_by_followup(env):
    cid = _competition()
    _remember(env)
    assert group_cleanup.archive_competition("g1", cid, operator="admin")
    before = db.get_competition(cid)
    _request_target(_handle(env))
    assert db.get_competition(cid) == before
    assert group_cleanup.is_competition_archived("g1", cid)
    assert _question_location("g2", before["name"]) == ""


def test_digest_with_multiple_targets_asks_which_target_without_partial_changes(env):
    first = _course(title="图论训练")
    second = _course(title="动态规划训练")
    cid = _competition()
    _remember(env)
    originals = [db.get_group_course("g1", first), db.get_group_course("g1", second),
                 db.get_competition(cid)]
    _request_target(_handle(env))
    assert [db.get_group_course("g1", first), db.get_group_course("g1", second),
            db.get_competition(cid)] == originals


def test_no_reference_can_choose_today_only_course_without_model(env):
    tid = _course()
    _changed(_handle(env, reference=""))
    assert db.get_group_course("g1", tid)["location"] == "93501"


def test_no_reference_ignores_non_today_and_other_group_courses(env):
    tid = _course(title="今天课程")
    tomorrow = _course(title="明天课程", weekday="周五")
    foreign = _course("g2", title="外群课程")
    _changed(_handle(env, reference=""))
    assert db.get_group_course("g1", tid)["location"] == "93501"
    assert db.get_group_course("g1", tomorrow)["location"] == ""
    assert db.get_group_course("g2", foreign)["location"] == ""


def test_no_reference_and_multiple_today_targets_request_target(env):
    first = _course(title="图论训练")
    second = _course(title="动态规划训练")
    _request_target(_handle(env, reference=""))
    assert db.get_group_course("g1", first)["location"] == ""
    assert db.get_group_course("g1", second)["location"] == ""


@pytest.mark.parametrize("prefix", ["今天", "今晚", "补充一下", "改一下"])
def test_generic_location_followup_does_not_pick_next_weeks_course(env, prefix):
    tid = _course(title="下周一集训", weekday="周一")
    before = db.get_group_course("g1", tid)
    _request_target(_handle(env, text=f"{prefix}地点93501", reference=""))
    assert db.get_group_course("g1", tid) == before


def test_explicit_title_selects_one_of_multiple_today_targets(env):
    first = _course(title="图论训练")
    second = _course(title="动态规划训练")
    _changed(_handle(env, text="图论训练 地点 B302", reference=""))
    assert db.get_group_course("g1", first)["location"] == "B302"
    assert db.get_group_course("g1", second)["location"] == ""


def test_same_title_in_multiple_target_types_is_ambiguous(env):
    tid = _course(title="训练选拔")
    cid = _competition(name="训练选拔")
    _request_target(_handle(env, text="训练选拔 地点 B302", reference=""))
    assert db.get_group_course("g1", tid)["location"] == ""
    assert _question_location("g1", db.get_competition(cid)["name"]) == ""


@pytest.mark.parametrize("kind", ["课程", "比赛"])
def test_typed_id_prevents_course_competition_id_collision(env, kind):
    tid = _course()
    cid = _competition()
    assert tid == cid
    before = db.get_competition(cid)
    _changed(_handle(env, text=f"{kind} {tid} 地点 B302", reference=""))
    if kind == "课程":
        assert db.get_group_course("g1", tid)["location"] == "B302"
        assert _question_location("g1", before["name"]) == ""
    else:
        assert db.get_group_course("g1", tid)["location"] == ""
        assert _question_location("g1", before["name"]) == "B302"
    assert db.get_competition(cid) == before


def test_typed_target_selects_correct_type_from_multi_target_digest(env):
    tid = _course()
    cid = _competition()
    assert tid == cid
    _remember(env)
    _changed(_handle(env, text=f"比赛 {cid} 地点 B302"))
    assert db.get_group_course("g1", tid)["location"] == ""
    assert _question_location("g1", db.get_competition(cid)["name"]) == "B302"


def test_explicit_course_id_is_still_group_scoped(env):
    foreign = _course("g2")
    before = db.get_group_course("g2", foreign)
    _request_target(_handle(env, text=f"课程 {foreign} 地点 B302", reference=""))
    assert db.get_group_course("g2", foreign) == before


def test_competition_location_is_group_overlay_visible_in_question_context(env):
    cid = _competition()
    before = db.get_competition(cid)
    _remember(env)
    _changed(_handle(env))
    assert db.get_competition(cid) == before
    assert _question_location("g1", before["name"]) == "93501"
    assert _question_location("g2", before["name"]) == ""
    db.init_db()
    assert _question_location("g1", before["name"]) == "93501"
    assert _question_location("g2", before["name"]) == ""


def test_same_followup_message_replay_does_not_write_again(env):
    tid = _course()
    _remember(env)
    _changed(_handle(env))
    before = db.get_group_course("g1", tid)
    audits = db.list_audit("g1", limit=100)
    _unchanged(_handle(env))
    assert db.get_group_course("g1", tid) == before
    assert db.list_audit("g1", limit=100) == audits


def test_replayed_msg_id_cannot_replace_location_with_different_body(env):
    tid = _course()
    _remember(env)
    _changed(_handle(env))
    before = db.get_group_course("g1", tid)
    _unchanged(_handle(env, text="地点改成A999", reference=""))
    assert db.get_group_course("g1", tid) == before


def test_followup_message_id_is_namespaced_by_group(env):
    first = _course("g1", title="一群课程")
    second = _course("g2", title="二群课程")
    _remember(env, "g1")
    _remember(env, "g2")
    _changed(_handle(env, group="g1", text="地点在B302"))
    _changed(_handle(env, group="g2", text="地点在A101"))
    assert db.get_group_course("g1", first)["location"] == "B302"
    assert db.get_group_course("g2", second)["location"] == "A101"


def test_saved_digest_reference_survives_module_reload_and_database_restart(env):
    tid = _course()
    _remember(env)
    db.init_db()
    restarted = importlib.reload(env)
    _changed(_handle(restarted))
    assert db.get_group_course("g1", tid)["location"] == "93501"


def test_followup_idempotency_survives_module_reload_and_database_restart(env):
    tid = _course()
    _remember(env)
    _changed(_handle(env))
    before = db.get_group_course("g1", tid)
    audits = db.list_audit("g1", limit=100)
    db.init_db()
    restarted = importlib.reload(env)
    _unchanged(_handle(restarted, text="地点改成A999", reference=""))
    assert db.get_group_course("g1", tid) == before
    assert db.list_audit("g1", limit=100) == audits


def test_yesterdays_digest_cannot_modify_todays_recurring_course(env):
    tid = _course()
    _remember(env)
    before = db.get_group_course("g1", tid)
    _request_target(_handle(env, now=NOW + timedelta(days=1)))
    assert db.get_group_course("g1", tid) == before


def test_reference_to_course_that_has_ended_cannot_update_location(env):
    tid = _course()
    _remember(env)
    before = db.get_group_course("g1", tid)
    _request_target(_handle(env, now=NOW.replace(hour=21, minute=1)))
    assert db.get_group_course("g1", tid) == before


def test_reference_to_competition_that_has_started_cannot_update_location(env):
    cid = _competition()
    _remember(env)
    before = db.get_competition(cid)
    _request_target(_handle(env, now=NOW.replace(hour=19, minute=1)))
    assert db.get_competition(cid) == before
    assert _question_location("g1", before["name"]) == ""


def test_empty_success_message_id_does_not_create_reference_context(env):
    tid = _course()
    detail = daily_digest.collect_daily_digest("g1", NOW)
    env.remember_digest("g1", "", detail, now=NOW)
    with db._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM business_message_context").fetchone()[0] == 0
    assert db.get_group_course("g1", tid)["location"] == ""


@pytest.mark.parametrize("text", ["", "地点在哪里", "今天几点上课", "93501", "地点"])
def test_non_location_statement_does_not_modify_any_course(env, text):
    tid = _course()
    _remember(env)
    before = db.get_group_course("g1", tid)
    _unchanged(_handle(env, text=text))
    assert db.get_group_course("g1", tid) == before


@pytest.mark.parametrize("text", [
    "地点是B302吗", "地点是B302吧", "地点暂定B302",
    "地点是B302还是A101", "地点在B302，但是还没确定",
    "听说图论训练地点在B302", "我猜图论训练地点是B302",
    "今天地点对吗", "图论训练地点从B302改成A101",
    "地点不用B302", "地点不在B302", "地点是B302或者A101",
    "地点是否B302", "地点是B302呢", "地点B302；时间待定",
    "图论训练地点可能在B302\n还没确定", "地点" + "长" * 50,
    "地点不改B302", "取消图论训练地点B302",
    "地点不一定在B302", "地点或许在B302", "地点估计在B302",
    "图论训练取消了 地点B302",
])
def test_uncertain_or_compound_location_is_not_a_business_patch(env, text):
    tid = _course(title="图论训练", location="旧地点")
    _remember(env)
    before = db.get_group_course("g1", tid)
    result = _handle(env, text=text)
    assert result["changed"] is False
    assert "确认" in result["reply"]
    assert db.get_group_course("g1", tid) == before
    assert db.list_audit("g1", limit=100) == []


def test_competition_location_change_between_digest_collect_and_send_invalidates_reference(env):
    cid = _competition()
    assert env.set_competition_location("g1", "teacher", cid, "A101", now=NOW)
    detail = daily_digest.collect_daily_digest("g1", NOW)
    assert detail["competitions"][0]["location"] == "A101"
    assert env.set_competition_location("g1", "teacher", cid, "B202", now=NOW)
    env.remember_digest("g1", "digest-1", detail, now=NOW)
    _request_target(_handle(env, text="地点在C303"))
    assert _question_location("g1", db.get_competition(cid)["name"]) == "B202"


@pytest.mark.parametrize("title", ["每周图论训练", "明天课程", "周四集训"])
def test_exact_existing_title_can_contain_date_words(env, title):
    tid = _course(title=title)
    _changed(_handle(env, text=f"{title} 地点B302", reference=""))
    assert db.get_group_course("g1", tid)["location"] == "B302"


def test_extra_words_cannot_turn_partial_title_into_an_update_target(env):
    tid = _course(title="图论训练")
    before = db.get_group_course("g1", tid)
    _request_target(_handle(env, text="关于图论训练的消息 地点B302", reference=""))
    assert db.get_group_course("g1", tid) == before


@pytest.mark.parametrize("kind,hour", [("course", 21), ("contest", 19)])
def test_reference_at_exact_end_or_contest_start_is_no_longer_writable(env, kind, hour):
    oid = _course() if kind == "course" else _competition()
    _remember(env)
    _request_target(_handle(env, now=NOW.replace(hour=hour)))
    if kind == "course":
        assert db.get_group_course("g1", oid)["location"] == ""
    else:
        assert _question_location("g1", db.get_competition(oid)["name"]) == ""


def test_typed_id_is_not_limited_by_chat_context_item_budget(env):
    first = _course(title="第一项")
    for index in range(14):
        _course(title=f"其他训练{index}")
    _changed(_handle(env, text=f"课程 {first} 地点B302", reference=""))
    assert db.get_group_course("g1", first)["location"] == "B302"


def test_location_followup_uses_normalized_occurrence_but_preserves_stored_time(env):
    tid = _course(start_time="19-21点", end_time="")
    result = _handle(env, text=f"课程 {tid} 地点B302", reference="")
    _changed(result)
    after = db.get_group_course("g1", tid)
    assert after["start_time"] == "19-21点" and after["end_time"] == ""
    assert "19:00–21:00" in result["reply"]
