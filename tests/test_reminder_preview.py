"""提醒预览不写库、不调用模型；名称、节点、队列与群范围保持一致。"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from cdutetc import commands, config, database as db, reminder_view

NOW = datetime(2026, 11, 8, 18, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "preview.sqlite3"))
    monkeypatch.setattr(db, "_delivery_now", lambda value=None: value or NOW)
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"team", "new"})
    monkeypatch.setattr(config, "WEEKLY_CONTEST_GROUPS", {"team"})
    monkeypatch.setattr(config, "CONTEST_REMIND_OFFSETS_MINUTES", (30, 15))
    monkeypatch.setattr(config, "REMIND_OFFSET_HOURS", (24, 1))
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    db.init_db()
    cid, _ = db.create_weekly_competition("2026-11-02", {"name": "集训周赛", "official_time": "2026-11-08 19:00"})
    return cid


def test_weekly_preview_shows_real_nodes_and_no_cross_group_data(store):
    text = reminder_view.render("team", now=NOW)
    assert "11-08 18:30（开赛 11-08 19:00）｜集训周赛 (#%d)｜开赛前30分钟｜待发送" % store in text
    assert "11-08 18:45（开赛 11-08 19:00）｜集训周赛 (#%d)｜开赛前15分钟｜待发送" % store in text
    other = reminder_view.render("new", now=NOW)
    assert "集训周赛" not in other and "暂无提醒计划" in other


def test_preview_includes_unqueued_course_and_collection_without_creating_deliveries(store):
    db.create_collection("team", "队服征集", deadline="2026-11-10 12:00")
    db.create_collection("new", "其他群征集", deadline="2026-11-10 12:00")
    db.create_training_session("team", {"title": "算法课", "weekday": "周一", "start_time": "19:00"})
    before = db.list_deliveries("team")
    text = reminder_view.render("team", now=NOW)
    assert "队服征集" in text and "算法课" in text and "待扫描" in text
    assert "其他群征集" not in text
    assert db.list_deliveries("team") == before


def test_preview_honors_receipt_state_and_detects_missing_queue(store):
    with db._connect() as conn:
        conn.execute("UPDATE reminder_deliveries SET status='sent' WHERE scheduled_at='2026-11-08 18:30:00'")
        conn.execute("DELETE FROM reminder_deliveries WHERE scheduled_at='2026-11-08 18:45:00'")
    text = reminder_view.render("team", now=NOW)
    assert "开赛前30分钟｜已发送" in text
    assert "开赛前15分钟｜未排队" in text
    assert "不能当作已安排" in text


def test_current_grace_window_is_visible_before_real_send(store):
    text = reminder_view.render("team", now=NOW.replace(minute=30, second=15))
    assert "开赛前30分钟｜待发送" in text
    assert "开赛前15分钟｜待发送" in text


def test_persisted_schedule_is_displayed_instead_of_recalculating_the_queue(store):
    with db._connect() as conn:
        conn.execute("UPDATE reminder_deliveries SET scheduled_at='2026-11-08 18:40:00', expires_at='2026-11-08 18:42:00'"
                     " WHERE scheduled_at='2026-11-08 18:30:00'")
    text = reminder_view.render("team", now=NOW)
    assert "11-08 18:40（开赛 11-08 19:00）｜集训周赛" in text
    assert "11-08 18:30｜集训周赛" not in text


def test_preview_pages_do_not_drop_plans(store):
    for i in range(7):
        db.create_collection("team", f"征集{i}", deadline="2026-11-10 12:00")
    one = reminder_view.render("team", page=1, now=NOW)
    two = reminder_view.render("team", page=2, now=NOW)
    assert "第1/2页" in one and "第2/2页" in two
    assert "下一页：/提醒状态 2" in one
    assert "共 2 页" in reminder_view.render("team", page=3, now=NOW)


def test_member_cannot_read_reminder_management_view(store):
    assert "仅老师/超管" in commands.cmd_reminder_status("team", "member", "")[0]["text"]
    assert "用法" in commands.cmd_reminder_status("team", "teacher", "乱填")[0]["text"]


def test_queue_records_resolve_target_names_and_sort_by_fire_time(store):
    """队列记录还原「名称 (#ID)」并按触发时间由近到远，与写入/主键顺序无关。"""
    late = db.create_collection("team", "队服补订", deadline="2026-11-20 14:00")
    early = db.create_collection("team", "设备借用", deadline="2026-11-18 14:00")
    db.ensure_delivery("team", "collection", late, "deadline", "2026-11-20 14:00")
    db.ensure_delivery("team", "collection", early, "deadline", "2026-11-18 14:00")
    with db._connect() as conn:
        conn.execute("UPDATE reminder_deliveries SET scheduled_at='2026-11-20 13:00:00'"
                     " WHERE obj_type='collection' AND obj_id=?", (late,))
        conn.execute("UPDATE reminder_deliveries SET scheduled_at='2026-11-18 13:00:00'"
                     " WHERE obj_type='collection' AND obj_id=?", (early,))
    text = reminder_view.render("team", now=NOW)
    assert "7天后已排期队列 2 项" in text
    assert f"设备借用 (#{early})" in text and f"队服补订 (#{late})" in text
    assert "collection#" not in text and "competition#" not in text
    assert text.index("11-18 13:00") < text.index("11-20 13:00")
    assert text.index(f"设备借用 (#{early})") < text.index(f"队服补订 (#{late})")


def test_seven_day_items_are_not_repeated_in_far_queue(store):
    """7 天内的待发送提醒只出现在上半部分，远期队列不重复展示同一场比赛。"""
    text = reminder_view.render("team", now=NOW)
    assert text.count(f"集训周赛 (#{store})") == 2
    assert "7天后已排期队列" not in text


def test_failed_queue_is_highlighted_first_and_deleted_target_has_fallback(store):
    """重试耗尽的记录置顶高亮；目标已删除时退回「已删除征集 (#ID)」。"""
    gone = db.create_collection("team", "临时征集", deadline="2026-11-19 14:00")
    db.ensure_delivery("team", "collection", gone, "deadline", "2026-11-19 14:00")
    with db._connect() as conn:
        conn.execute("UPDATE reminder_deliveries SET scheduled_at='2026-11-19 13:00:00', status='failed',"
                     " error_code='rate_limit' WHERE obj_type='collection' AND obj_id=?", (gone,))
        conn.execute("DELETE FROM collections WHERE id = ?", (gone,))
    text = reminder_view.render("team", now=NOW)
    assert "异常队列 1 项（重试耗尽 1）" in text
    assert f"已删除征集 (#{gone})" in text
    assert "错误 [rate_limit]" in text
    assert text.index("异常队列") < text.index("11-08 18:30")
