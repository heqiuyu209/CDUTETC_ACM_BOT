"""规格 B/C：群课程的新增、列表、修改与群级课程提醒开关。

全部走真实 sqlite 临时库（不 mock 数据层），只把库路径与白名单指到临时环境，
断言落库后的字段值、命令文案与提醒链路的一致性。
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from cdutetc import commands, config, database as db, group_admin, reminder
from cdutetc.utils import can_manage_group

TZ = ZoneInfo("Asia/Shanghai")
GID = "123456"
TEACHER = "654321"
OUTSIDER = "999999"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离库 + 白名单：老师 TEACHER 可管课，OUTSIDER 为普通成员。"""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "course.sqlite3"))
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "course.sqlite3"), raising=False)
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {GID}, raising=False)
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {TEACHER}, raising=False)
    monkeypatch.setattr(config, "SUPER_ADMIN", {"root"}, raising=False)
    db.init_db()
    return GID


def _text(result: list[dict]) -> str:
    return "\n".join(item["text"] for item in result)


def test_permission_helpers_cover_teacher_and_outsider(env):
    """老师可管课、普通成员不可管课，权限判定不含 Q 群身份。"""
    assert can_manage_group(TEACHER) is True
    assert can_manage_group(OUTSIDER) is False


def test_add_course_normalizes_dirty_time(env):
    """「周四 19-21点」这类脏时间入库前必须归一化成 19:00/21:00。"""
    text = _text(commands.cmd_training_add(GID, TEACHER, "训练 周四 19-21点 B201"))
    assert "已添加课程" in text
    assert "周四 19:00-21:00" in text
    course = db.list_training_sessions(GID)[0]
    assert (course["weekday"], course["start_time"], course["end_time"]) == ("周四", "19:00", "21:00")
    assert course["location"] == "B201"


def test_add_course_supports_named_fields(env):
    """命名参数（标题=/时间=/地点=）与位置参数等价。"""
    text = _text(commands.cmd_training_add(
        GID, TEACHER, "标题=算法特训 时间=每周四 19:30-21:30 地点=实训楼B302"))
    assert "算法特训" in text and "19:30-21:30" in text
    course = db.get_group_course(GID, 1)
    assert course["title"] == "算法特训"
    assert (course["start_time"], course["end_time"]) == ("19:30", "21:30")
    assert course["location"] == "实训楼B302"


def test_add_course_denied_for_normal_member(env):
    """普通成员添加课程被拒绝，且不落库。"""
    text = _text(commands.cmd_training_add(GID, OUTSIDER, "训练 周四 19-21点 B201"))
    assert "仅老师/超管可添加课程" in text
    assert db.list_training_sessions(GID) == []


def test_add_course_without_title_shows_usage(env):
    text = _text(commands.cmd_training_add(GID, TEACHER, "时间=周四 19:00-21:00"))
    assert "用法" in text or "格式" in text
    assert db.list_training_sessions(GID) == []


def test_list_courses_shows_normalized_span_and_requirement(env):
    """列表展示归一化时间与「要求」；未填要求时不渲染该段。"""
    commands.cmd_training_add(GID, TEACHER, "训练 周四 19-21点 B201 要求=带电脑")
    commands.cmd_training_add(GID, TEACHER, "自习 周三 20点")
    text = _text(commands.cmd_training_list(GID, OUTSIDER, ""))
    assert "19:00-21:00" in text
    assert "周三 20:00" in text
    assert "要求：带电脑" in text
    # 没写要求的那门课不出现空「要求：」段
    rows = [line for line in text.splitlines() if line.startswith("1. ")]
    assert rows and "要求" not in rows[0]


def test_list_courses_empty_notice(env):
    assert "暂无课程安排" in _text(commands.cmd_training_list(GID, OUTSIDER, ""))


def test_modify_by_index_updates_time(env):
    """用列表序号定位课程并改时间，落库字段与回执同步更新。"""
    commands.cmd_training_add(GID, TEACHER, "训练 周四 19-21点 B201")
    text = _text(commands.cmd_training_modify(GID, TEACHER, "1 时间=周三 20:00-22:00"))
    assert "已更新课程「训练」" in text and "20:00-22:00" in text
    course = db.get_group_course(GID, 1)
    assert (course["weekday"], course["start_time"], course["end_time"]) == ("周三", "20:00", "22:00")


def test_modify_by_title_updates_fields(env):
    """用课程名定位，并支持 电脑=/软件= 等附加字段。"""
    commands.cmd_training_add(GID, TEACHER, "算法特训 周四 19-21点 B201")
    text = _text(commands.cmd_training_modify(GID, TEACHER, "算法特训 电脑=需要 软件=PyCharm"))
    assert "需带电脑=需要" in text and "PyCharm" in text
    course = db.get_group_course(GID, 1)
    assert course["needs_laptop"] == 1
    assert course["software"] == "PyCharm"


def test_modify_ambiguous_title_reports_conflict(env):
    """同名课程命中多门时要求改用序号，绝不瞎猜改哪一门。"""
    commands.cmd_training_add(GID, TEACHER, "训练 周四 19-21点 B201")
    commands.cmd_training_add(GID, TEACHER, "训练 周五 19-21点 B202")
    text = _text(commands.cmd_training_modify(GID, TEACHER, "训练 地点=B303"))
    assert "匹配到多门课程" in text


def test_modify_unknown_course_reports_not_found(env):
    commands.cmd_training_add(GID, TEACHER, "训练 周四 19-21点 B201")
    text = _text(commands.cmd_training_modify(GID, TEACHER, "不存在的课 地点=B303"))
    assert "找不到课程" in text


def test_modify_denied_for_normal_member(env):
    commands.cmd_training_add(GID, TEACHER, "训练 周四 19-21点 B201")
    text = _text(commands.cmd_training_modify(GID, OUTSIDER, "1 地点=B303"))
    assert "仅老师/超管可修改课程" in text
    assert db.get_group_course(GID, 1)["location"] == "B201"


def test_group_course_remind_switch_gates_reminders(env):
    """群级课程提醒开关默认开启；关闭后提醒链路不再产出课程提醒。"""
    db.create_training_session(
        GID, {"title": "训练", "weekday": "周四", "start_time": "19:00",
              "end_time": "21:00", "location": "B201"}, TEACHER)
    now = datetime(2026, 11, 12, 18, 45, tzinfo=TZ)  # 周四开课前 15 分钟
    sessions = db.list_training_sessions(GID, active_only=False)
    assert db.group_course_remind_enabled(GID) is True
    assert reminder.build_training_reminders(sessions, now)

    db.set_group_course_remind(GID, False, TEACHER)
    assert db.group_course_remind_enabled(GID) is False
    assert reminder.build_training_reminders(sessions, now) == []

    db.set_group_course_remind(GID, True, TEACHER)
    assert reminder.build_training_reminders(sessions, now)


def test_group_admin_describe_reflects_remind_switch(env):
    """/群状态 的课程提醒状态与实际开关一致。"""
    assert "课程提醒：已开启" in group_admin.describe(GID)
    db.set_group_course_remind(GID, False, TEACHER)
    assert "课程提醒：已关闭" in group_admin.describe(GID)
