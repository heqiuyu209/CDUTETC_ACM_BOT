"""本群通知列表命令（/通知列表、/通知）的聚合、空数据与权限可见性用例。"""

import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from cdutetc import commands, config, database

TZ = ZoneInfo("Asia/Shanghai")
FUTURE = (datetime.now(TZ) + timedelta(days=3)).strftime("%Y-%m-%d %H:%M")
PAST = (datetime.now(TZ) - timedelta(days=2)).strftime("%Y-%m-%d %H:%M")


@pytest.fixture
def memory_db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    database.init_db()
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def roles(monkeypatch):
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2"})


def _seed_group(gid: str = "g1") -> None:
    database.create_competition({"name": "ACM校赛", "official_time": FUTURE,
                                 "official_reg_deadline": FUTURE, "location": "机房A"},
                                status="confirmed")
    database.create_competition({"name": "已结束的旧比赛", "official_time": PAST},
                                status="confirmed")
    database.create_org_event({"title": "期中考试安排", "contest_time": FUTURE,
                               "location": "教学楼B", "status": "confirmed",
                               "shareable": 1})
    database.create_training_session(gid, {"title": "算法训练", "weekday": "周三",
                                           "start_time": "19:00", "end_time": "21:00",
                                           "location": "机房", "needs_laptop": True})
    database.create_collection(gid, "周末训练", deadline=FUTURE)


async def test_notice_list_aggregates_four_kinds_of_records(memory_db):
    _seed_group()
    reply = await commands.handle_command("/通知列表", "g1", "student")
    assert len(reply) == 1
    text = reply[0]["text"]
    assert reply[0]["group_id"] == "g1"
    assert "error_code" not in reply[0]
    # 四类通知各成一节
    for title in ("【比赛】", "【学校安排】", "【课程安排】", "【报名中的征集】"):
        assert title in text
    # 内容沿用现有命令的时间字段口径
    assert "ACM校赛" in text and f"比赛 {FUTURE}" in text
    assert "期中考试安排" in text and "教学楼B" in text
    assert "算法训练（需带电脑）｜周三 19:00-21:00｜机房" in text
    assert "周末训练" in text and f"截止 {FUTURE}" in text
    # 已过期记录不进近期通知
    assert "已结束的旧比赛" not in text


async def test_notice_list_isolates_other_groups(memory_db):
    _seed_group("g1")
    other = database.create_competition({"name": "外群比赛", "official_time": FUTURE},
                                        status="confirmed")
    database.set_competition_notify_groups(other, ["g2"])
    database.create_training_session("g2", {"title": "外群课程"})
    text = (await commands.handle_command("/通知列表", "g1", "student"))[0]["text"]
    assert "外群比赛" not in text and "外群课程" not in text


async def test_notice_alias_returns_same_text(memory_db):
    _seed_group()
    full = (await commands.handle_command("/通知列表", "g1", "student"))[0]["text"]
    alias = (await commands.handle_command("/通知", "g1", "student"))[0]["text"]
    assert alias == full


async def test_notice_list_empty_group_gets_friendly_hint(memory_db):
    reply = await commands.handle_command("/通知列表", "g1", "student")
    text = reply[0]["text"]
    assert "本群近期暂无通知" in text
    assert "/帮助" in text
    for bad in ("错误", "失败", "异常", "error_code"):
        assert bad not in text
    assert "error_code" not in reply[0]


async def test_notice_list_allowed_for_plain_member(memory_db):
    _seed_group()
    text = (await commands.handle_command("/通知列表", "g1", "student"))[0]["text"]
    assert "仅老师" not in text and "仅超管" not in text
    assert "ACM校赛" in text


def test_notice_list_listed_in_help_sections_for_members():
    member_help = commands.build_help("student", "全部")
    assert "/通知列表" in member_help
    # 与"学校安排"同节，普通成员用 /帮助 课程 也能看到
    assert "/通知列表" in commands.build_help("student", "课程")
    # level 1 命令不依赖管理身份，且不因此暴露管理命令
    assert "/加群" not in member_help and "/修正" not in member_help
    assert commands.build_help("student").count("/通知列表") == 0
