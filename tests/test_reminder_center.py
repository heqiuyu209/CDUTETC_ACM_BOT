"""统一主动提醒中心：清单聚合、增删改、时间解析。"""

import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from cdutetc import commands, config, database
from cdutetc.config import TZ_NAME

TZ = ZoneInfo(TZ_NAME)
NOW = datetime(2026, 11, 12, 10, 0, tzinfo=TZ)  # 周四


@pytest.fixture
def env(monkeypatch):
    """内存库 + 固定时钟 + 单群白名单；提醒节点固定为 24h/1h。"""
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    monkeypatch.setattr(database, "_delivery_now", lambda value=None: value or NOW)
    monkeypatch.setattr(commands, "_now_local", lambda: NOW)
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "REMIND_OFFSET_HOURS", (24.0, 1.0))
    monkeypatch.setattr(config, "TRAINING_REMIND_EXTRA_OFFSETS_MINUTES", ())
    database.init_db()
    yield conn
    conn.close()


def _seed_course(group_id="g1"):
    """今天 19:00 开课，1 小时节点在今天 18:00 命中。"""
    return database.create_training_session(group_id, {
        "title": "算法集训", "weekday": "周四", "start_time": "19:00",
        "end_time": "21:00", "location": "实验楼 302", "status": "confirmed",
    }, operator="teacher")


def _seed_contest(group_id="g1"):
    """两小时后开赛，命中 1 小时节点。"""
    official = (NOW + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
    cid = database.create_competition({
        "name": "蓝桥杯校赛", "official_time": official, "official_site": "https://example.com",
    }, status="confirmed", operator="teacher")
    database.set_competition_notify_groups(cid, [group_id])
    return cid


def _seed_custom(group_id="g1", content="交作业", target="2026-11-12 12:00",
                 repeat_rule="none", weekday="", time_of_day=""):
    return database.create_custom_reminder(
        group_id, content, target, repeat_rule=repeat_rule,
        weekday=weekday, time_of_day=time_of_day, operator="teacher",
    )


# ---------------------------------------------------------------- 清单聚合

def test_list_shows_all_three_sources_with_labels(env):
    _seed_course()
    _seed_contest()
    _seed_custom()

    text = commands.cmd_reminder_list("g1", "teacher", "")[0]["text"]

    assert "未来 7 天提醒" in text
    assert "[课程]" in text and "算法集训" in text
    assert "[比赛]" in text and "蓝桥杯校赛" in text
    assert "[自定义]" in text and "交作业" in text


def test_list_marks_weekly_custom_reminder(env):
    _seed_custom(content="每周交作业", target="2026-11-12 12:00",
                 repeat_rule="weekly", weekday="四", time_of_day="12:00")

    text = commands.cmd_reminder_list("g1", "teacher", "")[0]["text"]

    assert "[自定义]" in text
    assert "（每周）" in text


def test_list_empty_group_shows_add_hint(env):
    text = commands.cmd_reminder_list("g1", "teacher", "")[0]["text"]
    assert "暂无提醒" in text
    assert "/提醒添加" in text


def test_list_hides_sources_disabled_by_group_switch(env):
    _seed_course()
    _seed_contest()
    _seed_custom()
    database.set_group_switch("g1", "contest_remind_enabled", False, "teacher")
    database.set_group_switch("g1", "course_remind_enabled", False, "teacher")

    text = commands.cmd_reminder_list("g1", "teacher", "")[0]["text"]

    assert "[比赛]" not in text
    assert "[课程]" not in text
    # 自定义提醒不受群开关约束，仍应展示
    assert "[自定义]" in text


def test_list_requires_group_admin(env):
    replies = commands.cmd_reminder_list("g1", "m1", "")
    assert "提醒中心仅限本群老师或超级管理员使用" in replies[0]["text"]


@pytest.mark.parametrize("args", ["abc", "0", "31"])
def test_list_rejects_bad_days(env, args):
    with pytest.raises(ValueError):
        commands.cmd_reminder_list("g1", "teacher", args)


def test_list_honours_custom_horizon(env):
    # 20 天后到期的提醒：默认 7 天看不到，指定 30 天可见
    _seed_custom(content="远期提醒", target="2026-12-02 12:00")
    assert "远期提醒" not in commands.cmd_reminder_list("g1", "teacher", "")[0]["text"]
    assert "远期提醒" in commands.cmd_reminder_list("g1", "teacher", "30")[0]["text"]


# ---------------------------------------------------------------- 时间解析

def test_parse_absolute_time(env):
    target, rule, weekday, time_of_day, rest = commands.parse_reminder_time(
        "2026-11-20 18:00 交作业", NOW)
    assert target == datetime(2026, 11, 20, 18, 0, tzinfo=TZ)
    assert rule == "none"
    assert weekday == ""
    assert time_of_day == "18:00"
    assert rest.strip(" ，,、:：") == "交作业"


def test_parse_weekly_time_keeps_today_when_not_passed(env):
    target, rule, weekday, time_of_day, rest = commands.parse_reminder_time(
        "每周四 19:00 交作业", NOW)
    assert target == datetime(2026, 11, 12, 19, 0, tzinfo=TZ)
    assert (rule, weekday, time_of_day) == ("weekly", "四", "19:00")
    assert rest.strip(" ，,、:：") == "交作业"


def test_parse_weekly_time_rolls_to_next_week(env):
    later = datetime(2026, 11, 12, 20, 30, tzinfo=TZ)  # 已过今天 19:00
    target, rule, _, _, _ = commands.parse_reminder_time("每周四 19:00 交作业", later)
    assert target == datetime(2026, 11, 19, 19, 0, tzinfo=TZ)
    assert rule == "weekly"


@pytest.mark.parametrize("prefix", ["明天 19:00", "每周四 25:00", "每周九 19:00"])
def test_parse_invalid_time_raises(env, prefix):
    with pytest.raises(ValueError):
        commands.parse_reminder_time(prefix, NOW)


# ---------------------------------------------------------------- 添加

def test_add_single_reminder_reports_next_time(env):
    text = commands.cmd_reminder_add("g1", "teacher", "2026-11-20 18:00 交作业")[0]["text"]

    assert "已添加提醒 #" in text
    assert "下次提醒：2026-11-20 18:00" in text
    rows = database.list_custom_reminders("g1")
    assert len(rows) == 1
    assert rows[0]["repeat_rule"] == "none"
    assert rows[0]["status"] == "pending"


def test_add_weekly_reminder_persists_rule(env):
    text = commands.cmd_reminder_add("g1", "teacher", "每周四 19:00 交作业")[0]["text"]

    assert "（每周重复）" in text
    row = database.list_custom_reminders("g1")[0]
    assert row["repeat_rule"] == "weekly"
    assert row["weekday"] == "四"
    assert row["time_of_day"] == "19:00"
    assert row["target_time"] == "2026-11-12 19:00"


def test_add_reminder_requires_admin(env):
    replies = commands.cmd_reminder_add("g1", "m1", "2026-11-20 18:00 交作业")
    assert "提醒中心仅限本群老师或超级管理员使用" in replies[0]["text"]
    assert database.list_custom_reminders("g1") == []


@pytest.mark.parametrize("args", ["", "2026-11-20 18:00", "2026-11-20 18:00   "])
def test_add_without_content_raises(env, args):
    with pytest.raises(ValueError):
        commands.cmd_reminder_add("g1", "teacher", args)


def test_add_with_unparsable_time_raises(env):
    with pytest.raises(ValueError):
        commands.cmd_reminder_add("g1", "teacher", "下周找个时间 交作业")


# ---------------------------------------------------------------- 修改

def test_modify_time_and_rule(env):
    _seed_custom(content="交作业", target="2026-11-12 12:00")
    text = commands.cmd_reminder_modify("g1", "teacher", "1 每周四 19:00 交作业")[0]["text"]

    assert "已更新提醒 #" in text
    assert "（每周重复）" in text
    assert "下次提醒：2026-11-12 19:00" in text
    row = database.list_custom_reminders("g1")[0]
    assert row["repeat_rule"] == "weekly"
    assert row["target_time"] == "2026-11-12 19:00"


def test_modify_content_only_keeps_repeat_rule(env):
    _seed_custom(content="交作业", target="2026-11-12 12:00",
                 repeat_rule="weekly", weekday="四", time_of_day="12:00")
    text = commands.cmd_reminder_modify("g1", "teacher", "1 交实验报告")[0]["text"]

    assert "已更新提醒 #" in text
    assert "交实验报告" in text
    row = database.list_custom_reminders("g1")[0]
    assert row["content"] == "交实验报告"
    assert row["repeat_rule"] == "weekly"


def test_modify_rejects_non_custom_entry(env):
    _seed_course()
    with pytest.raises(ValueError) as err:
        commands.cmd_reminder_modify("g1", "teacher", "1 每周四 19:00 改内容")
    assert "只能修改自定义提醒" in str(err.value)


def test_modify_requires_selector_and_payload(env):
    with pytest.raises(ValueError):
        commands.cmd_reminder_modify("g1", "teacher", "1")


def test_modify_requires_admin(env):
    replies = commands.cmd_reminder_modify("g1", "m1", "1 交作业")
    assert "提醒中心仅限本群老师或超级管理员使用" in replies[0]["text"]


# ---------------------------------------------------------------- 删除

def test_delete_by_index_removes_reminder(env):
    _seed_custom(content="交作业", target="2026-11-12 12:00")
    text = commands.cmd_reminder_delete("g1", "teacher", "1")[0]["text"]

    assert "已删除提醒 #" in text
    assert "交作业" in text
    assert database.list_custom_reminders("g1") == []


def test_delete_by_hash_id(env):
    rid = _seed_custom(content="交作业", target="2026-11-12 12:00")
    text = commands.cmd_reminder_delete("g1", "teacher", f"#{rid}")[0]["text"]
    assert "已删除提醒 #" in text
    assert database.list_custom_reminders("g1") == []


@pytest.mark.parametrize("args", ["", "9", "abc"])
def test_delete_rejects_bad_selector(env, args):
    _seed_custom(content="交作业", target="2026-11-12 12:00")
    with pytest.raises(ValueError):
        commands.cmd_reminder_delete("g1", "teacher", args)


def test_delete_requires_admin(env):
    _seed_custom(content="交作业", target="2026-11-12 12:00")
    replies = commands.cmd_reminder_delete("g1", "m1", "1")
    assert "提醒中心仅限本群老师或超级管理员使用" in replies[0]["text"]
    assert len(database.list_custom_reminders("g1")) == 1
