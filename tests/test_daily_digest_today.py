"""「当日群隔离总览」晨报 + 课程提醒地点兜底的回归测试。

覆盖：
- 课程地点为 None / 空串 / 纯空白时，主动提醒渲染「地点：待定」，且
  「要求：」独立成行，绝不与其他内容粘连；
- 周四晨报只收周四课程与当日比赛，周日周赛不得混入；
- 周日晨报收录周赛，且只对周赛订阅群可见（非订阅群不出现周赛）；
- 群当日无任何活动时返回 None（不发空消息、不占主动额度）；
- daily_digest_enabled / contest_remind_enabled / course_remind_enabled
  任一关闭时，对应段落或整体被正确抑制。
"""

import asyncio
import logging
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cdutetc import config, daily_digest, database as db, main, reminder
from cdutetc.clients.qqbot import QQBotClient
from cdutetc.reminder import TZ

THU = datetime(2026, 11, 12, 8, 0, tzinfo=TZ)    # 周四 08:00
SUN = datetime(2026, 11, 15, 8, 0, tzinfo=TZ)    # 周日 08:00


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离库 + 白名单 + 固定时段配置，全部走真实 sqlite。"""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "digest_today.sqlite3"))
    monkeypatch.setattr(db, "_delivery_now", lambda value=None: value or THU)
    monkeypatch.setattr(config, "BOT_PROTOCOL", "qqbot")
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    monkeypatch.setattr(config, "DAILY_DIGEST_ENABLED", True)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_ENABLED", True)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_GROUPS", {"g1"})
    monkeypatch.setattr(config, "CONTEST_REMIND_OFFSETS_MINUTES", (30, 15))
    monkeypatch.setattr(config, "REMIND_OFFSET_HOURS", (24, 1))
    db.init_db()
    return db


def _course(group_id="g1", weekday="周四", start="19:00", end="21:00",
            title="周四集训", location="实训楼 B302", requirement="") -> int:
    return db.create_training_session(group_id, {
        "title": title, "weekday": weekday, "start_time": start, "end_time": end,
        "location": location, "requirement": requirement,
    })


def _contest(name="Codeforces Round 999", official_time="2026-11-12 22:00",
             group_id="g1", official_site="", edition="") -> int:
    info = {"name": name, "official_time": official_time}
    if official_site:
        info["official_site"] = official_site
    if edition:
        info["edition"] = edition
    cid = db.create_competition(info, status="confirmed")
    db.set_competition_notify_groups(cid, [group_id])
    return cid


def _weekly(week_start="2026-11-09", name="队内周赛", official_time="2026-11-15 19:00") -> int:
    cid = db.create_weekly_competition(week_start, {"name": name,
                                                    "official_time": official_time})[0]
    db.set_competition_notify_groups(cid, ["g1"])
    return cid


# ------------------------------------------------- A. 课程地点兜底与换行隔离

@pytest.mark.parametrize("location", [None, "", "   ", "\t\n"])
def test_training_reminder_renders_pending_location_on_own_line(env, location):
    """地点缺失时提醒里必须出现独立的「地点：待定」行，要求行紧随其后。"""
    _course(location=location, requirement="提交周报并现场讲评")
    sessions = db.list_training_sessions("g1")

    built = reminder.build_training_reminders(sessions, datetime(2026, 11, 12, 18, 0, tzinfo=TZ))

    assert len(built) == 1
    lines = built[0]["text"].split("\n")
    assert lines[0].startswith("【提醒】课程「周四集训」开始：2026-11-12 19:00")
    assert lines[1] == "地点：待定"
    assert lines[2] == "要求：提交周报并现场讲评"
    # 任何形式的粘连都不允许出现
    assert "待定要求" not in built[0]["text"]
    assert "地点：待定要求" not in built[0]["text"]


def test_training_reminder_keeps_location_when_present(env):
    """有地点时正常渲染，且仍然独占一行。"""
    _course(location="实训楼 B302", requirement="带电脑")
    built = reminder.build_training_reminders(
        db.list_training_sessions("g1"), datetime(2026, 11, 12, 18, 0, tzinfo=TZ)
    )
    lines = built[0]["text"].split("\n")
    assert lines[1] == "地点：实训楼 B302"
    assert lines[2] == "要求：带电脑"


# ------------------------------------------------- B. 周四晨报：当日 + 群隔离

def test_thursday_digest_only_today_courses_and_contests(env):
    _course(weekday="周四", title="周四集训", location="")
    _course(weekday="周日", title="周日集训")
    _contest(name="Codeforces Round 999", official_time="2026-11-12 22:00")
    _contest(name="牛客小白月赛", official_time="2026-11-13 22:00")   # 明天，不算今日
    _weekly(name="队内周赛", official_time="2026-11-12 19:00")        # 周四不推周赛

    text = daily_digest.build_daily_digest("g1", THU)

    assert text is not None
    assert "📚 今日课程/训练：" in text and "周四集训" in text
    assert "地点：待定" in text
    assert "周日集训" not in text                       # 非今日星期
    assert "Codeforces Round 999" in text               # 当日比赛在列
    assert "牛客小白月赛" not in text                    # 明日比赛不在列
    assert "队内周赛" not in text                        # 周一到周六不出现周赛


# ------------------------------------------------- C. 周日晨报：周赛仅订阅群

def test_sunday_digest_shows_weekly_only_to_subscribers(env):
    _weekly(name="队内周赛", official_time="2026-11-15 19:00")
    _course(group_id="g2", weekday="周日", title="二群周日集训")

    subscribed = daily_digest.build_daily_digest("g1", SUN)
    outsider = daily_digest.build_daily_digest("g2", SUN)

    assert "队内周赛" in subscribed
    assert outsider is not None and "二群周日集训" in outsider
    assert "队内周赛" not in outsider                    # 非订阅群不得出现周赛


# ------------------------------------------------- D. 全空不发送

def test_digest_returns_none_without_any_activity(env):
    assert daily_digest.build_daily_digest("g1", THU) is None
    assert daily_digest.build_daily_digest("", THU) is None


# ------------------------------------------------- E. 三段开关抑制

def test_digest_sections_follow_group_switches(env):
    _course(title="周四集训")
    _contest(name="Codeforces Round 999", official_time="2026-11-12 22:00")

    both = daily_digest.build_daily_digest("g1", THU)
    assert "周四集训" in both and "Codeforces Round 999" in both

    db.set_group_switch("g1", "course_remind_enabled", False, "teacher")
    only_contest = daily_digest.build_daily_digest("g1", THU)
    assert "周四集训" not in only_contest
    assert "Codeforces Round 999" in only_contest

    db.set_group_switch("g1", "contest_remind_enabled", False, "teacher")
    assert daily_digest.build_daily_digest("g1", THU) is None

    db.set_group_switch("g1", "course_remind_enabled", True, "teacher")
    only_course = daily_digest.build_daily_digest("g1", THU)
    assert "周四集训" in only_course
    assert "Codeforces Round 999" not in only_course


def test_digest_job_respects_global_switch(env, monkeypatch):
    _course(title="周四集训")
    api = SimpleNamespace(post_group_message=AsyncMock(return_value={"id": "sent"}))
    client = QQBotClient("offline-app", "offline-secret")
    client._client = SimpleNamespace(api=api)
    bot = main.ACMBot(client)

    monkeypatch.setattr(config, "DAILY_DIGEST_ENABLED", False)
    assert asyncio.run(bot._daily_digest_job(now=THU)) == 0
    api.post_group_message.assert_not_awaited()

    monkeypatch.setattr(config, "DAILY_DIGEST_ENABLED", True)
    assert asyncio.run(bot._daily_digest_job(now=THU)) == 1


# ------------------------------------------------- F. 今日筛选 + 无赛静默跳过

def test_digest_lists_only_today_when_two_today_and_one_tomorrow(env):
    """今日 2 场 + 明日 1 场：晨报只能出现今日 2 场，明日场次一个字都不能有。"""
    _contest(name="CF 今日 19:00 场", official_time="2026-11-12 19:00")
    _contest(name="CF 今日 23:30 场", official_time="2026-11-12 23:30")
    _contest(name="CF 明日 22:00 场", official_time="2026-11-13 22:00")

    detail = daily_digest.collect_daily_digest("g1", THU)

    assert detail["today_contest_count"] == 2
    text = detail["text"]
    assert "CF 今日 19:00 场" in text
    assert "CF 今日 23:30 场" in text
    assert "CF 明日 22:00 场" not in text


def test_digest_excludes_retracted_and_undated_contests(env):
    """被撤回为 draft（非 confirmed）与时间未定（无法解析 00:00:00–23:59:59）的场次都不进晨报。"""
    _contest(name="CF 今日正常场", official_time="2026-11-12 20:00")
    retracted = _contest(name="CF 今日撤回场", official_time="2026-11-12 21:00")
    db.set_competition_status(retracted, "draft", operator="admin")
    _contest(name="CF 今日待定场", official_time="")

    detail = daily_digest.collect_daily_digest("g1", THU)

    assert detail["today_contest_count"] == 1
    assert "CF 今日正常场" in detail["text"]
    assert "CF 今日撤回场" not in detail["text"]
    assert "CF 今日待定场" not in detail["text"]


def test_digest_job_silently_skips_when_no_contest_today(env, caplog):
    """今日 0 场（明天、后天都有）时必须静默跳过：不调发送接口、返回 0、只记 INFO。"""
    _contest(name="CF 明日 22:00 场", official_time="2026-11-13 22:00")
    _contest(name="CF 后天 22:00 场", official_time="2026-11-14 22:00")
    api = SimpleNamespace(post_group_message=AsyncMock(return_value={"id": "sent"}))
    client = QQBotClient("offline-app", "offline-secret")
    client._client = SimpleNamespace(api=api)
    bot = main.ACMBot(client)

    assert daily_digest.collect_daily_digest("g1", THU)["text"] is None
    with caplog.at_level(logging.INFO):
        assert asyncio.run(bot._daily_digest_job(now=THU)) == 0

    api.post_group_message.assert_not_awaited()
    messages = [record.getMessage() for record in caplog.records]
    assert any("[晨报] 群 g1 今日无比赛，已跳过发送" in text for text in messages)
    assert any("[晨报] 群 g2 今日无比赛，已跳过发送" in text for text in messages)
    assert all("今日暂无比赛" not in text for text in messages)


# ------------------------------------------------- G. 按平台分组排版

def _groups_in(text: str) -> list[str]:
    """按出现顺序取出所有平台分隔行。"""
    return [line.strip("-").strip() for line in text.splitlines()
            if line.startswith("-----")]


def test_digest_groups_contests_by_platform(env):
    """五类平台各一场：分组标题按固定顺序出现，有链接才输出「链接：」行。"""
    _weekly(name="队内周赛", official_time="2026-11-15 19:00")   # 无链接
    _contest(name="牛客小白月赛 100", official_time="2026-11-15 20:00",
             official_site="https://ac.nowcoder.com/acm/contest/88888")
    _contest(name="Codeforces Round 2030", official_time="2026-11-15 22:35",
             official_site="https://codeforces.com/contest/2030")
    _contest(name="AtCoder Beginner Contest 380", official_time="2026-11-15 20:00",
             official_site="https://atcoder.jp/contests/abc380")
    _contest(name="蓝桥杯热身赛", official_time="2026-11-15 14:00")   # 无平台、无链接

    text = daily_digest.build_daily_digest("g1", SUN)

    assert text.splitlines()[0] == "☀️ CDUTETC ACM 今日早报（11-15 周日）"
    assert "🏆 今日赛事（5 场）" in text
    assert _groups_in(text) == ["CDUTETC_ACM", "牛客", "Codeforces", "AtCoder", "其他赛事"]
    # 每场都是「- HH:MM（倒计时后）」+ 名称两行结构
    assert "- 19:00（11 小时后）\n  队内周赛" in text
    assert "- 22:35（14 小时 35 分钟后）\n  Codeforces Round 2030" in text
    # 有链接的场次紧跟链接行
    assert "  链接：https://ac.nowcoder.com/acm/contest/88888" in text
    assert "  链接：https://codeforces.com/contest/2030" in text
    assert "  链接：https://atcoder.jp/contests/abc380" in text
    # 无链接的场次不能出现空的「链接：」标签
    cdutetc_block = text.split("-------牛客-------")[0]
    assert "队内周赛" in cdutetc_block and "链接：" not in cdutetc_block
    other_block = text.split("-------其他赛事-------")[1]
    assert "蓝桥杯热身赛" in other_block and "链接：" not in other_block


def test_digest_sorts_contests_inside_platform_group(env):
    """同一平台内按开赛时间升序，与入库顺序无关。"""
    _contest(name="Codeforces Round 991", official_time="2026-11-15 22:35",
             official_site="https://codeforces.com/contest/2031")
    _contest(name="Codeforces Round 990", official_time="2026-11-15 19:00",
             official_site="https://codeforces.com/contest/2030")

    text = daily_digest.build_daily_digest("g1", SUN)

    assert _groups_in(text) == ["Codeforces"]
    assert text.index("Codeforces Round 990") < text.index("Codeforces Round 991")


def test_digest_has_no_command_footer(env):
    """晨报结尾不得再出现分隔线与「发送对应命令…查看详情」引导。"""
    _course(title="周日集训")
    _contest(name="牛客小白月赛 100", official_time="2026-11-15 20:00",
             official_site="https://ac.nowcoder.com/acm/contest/88888")
    db.create_custom_reminder("g1", "交周报", "2026-11-15 21:00")

    text = daily_digest.build_daily_digest("g1", SUN)

    assert "发送对应命令" not in text
    assert "查看详情" not in text
    assert "------------------------" not in text
    assert text.rstrip().endswith("21:00 交周报")
