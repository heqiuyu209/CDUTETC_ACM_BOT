"""聚合晨报（daily_digest）与被动搭便车（piggyback）的行为验证。

覆盖：
- 三路数据源的时间窗口与过滤（今日课程 / 48h 内征集 / 24h 内且预检就绪比赛）；
- 全空不发消息（不浪费 1~4 条/天的主动额度）；
- 纯文本排版（不含 Markdown 语法，避免官方客户端拦截）；
- 搭便车的触发范围、最紧急优先级与单群 10 分钟冷却；
- 晨报任务遇 QUOTA_EXHAUSTED 跳过告警、不阻塞其余群。
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cdutetc import commands, config, daily_digest, database as db, main, reminder_preflight
from cdutetc.clients.qqbot import QQBotClient
from cdutetc.errors import DeliveryCode
from cdutetc.reminder import TZ

NOW = datetime(2026, 11, 11, 18, 0, tzinfo=TZ)  # 周三 18:00


class ServerError(RuntimeError):
    """模拟 botpy 的 ServerError，业务码放在 msgs/响应体里（与平台报错同构）。"""

    def __init__(self, payload):
        self.msgs = payload
        super().__init__(str(payload))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "digest.sqlite3"))
    monkeypatch.setattr(db, "_delivery_now", lambda value=None: value or NOW)
    monkeypatch.setattr(config, "BOT_PROTOCOL", "qqbot")
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_ENABLED", False)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_GROUPS", set())
    monkeypatch.setattr(config, "CONTEST_REMIND_OFFSETS_MINUTES", (30, 15))
    monkeypatch.setattr(config, "REMIND_OFFSET_HOURS", (24, 1))
    monkeypatch.setattr(config, "DAILY_DIGEST_ENABLED", True)
    monkeypatch.setattr(config, "DAILY_DIGEST_PIGGYBACK_ENABLED", True)
    db.init_db()
    commands.reset_piggyback_cooldown()
    yield
    commands.reset_piggyback_cooldown()


def _course(group_id="g1", weekday="周三", start="19:00", end="21:00",
            title="图论进阶", location="实训楼 B302") -> int:
    return db.create_training_session(group_id, {
        "title": title, "weekday": weekday, "start_time": start,
        "end_time": end, "location": location,
    })


def _collection(group_id="g1", title="天梯赛选拔", deadline="2026-11-12 12:00",
                capacity=None, status="open") -> int:
    cid = db.create_collection(group_id, title, deadline=deadline, capacity=capacity,
                               fields=["姓名", "学号"])
    if status != "open":
        with db._connect() as conn:
            conn.execute("UPDATE collections SET status=? WHERE id=?", (status, cid))
    return cid


def _contest(group_id="g1", name="Codeforces Round 990",
             official_time="2026-11-11 22:00", cancel_nodes=False) -> int:
    cid = db.create_competition({"name": name, "official_time": official_time},
                                status="confirmed")
    db.set_competition_notify_groups(cid, [group_id])
    if cancel_nodes:  # 模拟「查得到但到点不推」：节点已取消
        with db._connect() as conn:
            conn.execute("UPDATE reminder_deliveries SET status='cancelled'"
                         " WHERE group_id=? AND obj_type='competition' AND obj_id=?",
                         (group_id, cid))
    return cid


# --------------------------------------------------------------- 数据源收集

def test_today_sessions_lists_only_unfinished(env):
    today = _course(start="19:00", end="21:00", title="图论进阶")
    _course(start="09:00", end="11:00", title="早间模拟赛")  # 已结束
    _course(weekday="周五", start="19:00", end="21:00", title="周五训练")  # 非今日

    sessions = daily_digest.today_sessions("g1", NOW)
    assert [item["id"] for item in sessions] == [today]
    assert sessions[0]["start_text"] == "19:00"
    assert sessions[0]["end_text"] == "21:00"
    assert sessions[0]["ongoing"] is False


def test_soon_collections_window_and_capacity(env):
    soon = _collection(deadline="2026-11-12 12:00", capacity=3)  # 18h 后
    _collection(deadline="2026-11-20 12:00")  # 超出 48h
    _collection(deadline="2026-11-10 12:00")  # 已过期
    _collection(deadline="2026-11-12 12:00", status="closed")  # 非开放

    items = daily_digest.soon_collections("g1", NOW)
    assert [item["id"] for item in items] == [soon]
    assert items[0]["remaining"] == 3
    assert items[0]["fields"] == ["姓名", "学号"]


def test_soon_competitions_requires_preflight_ready(env):
    ready = _contest(official_time="2026-11-11 22:00")                     # 4h 后，节点就绪
    _contest(name="节点已取消", official_time="2026-11-11 23:00", cancel_nodes=True)
    _contest(name="明日之后", official_time="2026-11-13 22:00")             # 超出 24h

    items = daily_digest.soon_competitions("g1", NOW)
    assert [item["id"] for item in items] == [ready]
    assert reminder_preflight.check_contest_delivery_readiness(ready, "g1", NOW)[0] is True


def test_soon_competitions_skips_blocked_group(env):
    cid = _contest(official_time="2026-11-11 22:00")
    assert cid
    db.block_group_notifications("qqbot", "g1", DeliveryCode.QUOTA_EXHAUSTED.value, 11253)
    assert daily_digest.soon_competitions("g1", NOW) == []


# --------------------------------------------------------------- 排版与判空

def test_digest_is_none_when_everything_empty(env):
    assert daily_digest.build_daily_digest("g1", NOW) is None
    assert daily_digest.build_daily_digest("", NOW) is None


def test_digest_renders_plain_text_overview(env):
    _course()
    _collection(capacity=2)
    _contest()

    text = daily_digest.build_daily_digest("g1", NOW)

    assert text.startswith(daily_digest.DIGEST_TITLE)
    assert "📚 今日课程/训练：" in text
    assert "2026-11-11 19:00-21:00 图论进阶 (实训楼 B302)" in text
    assert "⏳ 征集截止：" in text and "剩 2 个名额" in text
    assert "🏆 近期赛事：" in text and "Codeforces Round 990" in text
    assert text.rstrip().endswith(daily_digest.DIGEST_FOOTER)
    for markdown in ("**", "##", "|", "`"):  # 纯文本，防官方客户端拦截
        assert markdown not in text


def test_digest_omits_empty_sections(env):
    _collection(deadline="2026-11-12 12:00")
    text = daily_digest.build_daily_digest("g1", NOW)
    assert "⏳ 征集截止：" in text
    assert "📚 今日课程/训练：" not in text
    assert "🏆 近期赛事：" not in text


# --------------------------------------------------------------- 搭便车

def test_piggyback_prefers_most_urgent(env):
    _collection(title="天梯赛选拔", deadline="2026-11-12 05:00", capacity=2)   # 11h 后
    _collection(title="周末讲座报名", deadline="2026-11-14 12:00")             # 超出 12h
    _contest(name="Codeforces Round 990", official_time="2026-11-11 19:30")   # 1.5h 后

    notice = daily_digest.build_piggyback_notice("g1", NOW)

    assert notice.startswith("---")
    assert "Codeforces Round 990" in notice and "事务速递" in notice


def test_piggyback_writes_resume_command_for_collection(env):
    cid = _collection(title="天梯赛选拔", deadline="2026-11-11 21:00")  # 3h 后

    notice = daily_digest.build_piggyback_notice("g1", NOW)

    assert f"/报名 {cid}" in notice
    assert "姓名=..." in notice


def test_piggyback_none_without_urgent_items(env):
    _collection(deadline="2026-11-13 12:00")  # 43h 后，超出 12h 阈值
    assert daily_digest.build_piggyback_notice("g1", NOW) is None


def test_append_piggyback_respects_cooldown(env):
    _collection(deadline="2026-11-11 21:00")

    first = commands.append_piggyback_notice("g1", "/帮助 内容", now=NOW)
    assert first.startswith("/帮助 内容")
    assert "事务速递" in first

    # 冷却期内（9 分钟后）不再重复追加
    again = commands.append_piggyback_notice("g1", "/FAQ列表 内容", now=NOW.replace(minute=9))
    assert again == "/FAQ列表 内容"

    # 冷却期满（11 分钟后）允许再次搭车
    third = commands.append_piggyback_notice("g1", "/课程列表 内容", now=NOW.replace(minute=11))
    assert "事务速递" in third


def test_append_piggyback_noop_cases(env):
    _collection(deadline="2026-11-11 21:00")
    assert commands.append_piggyback_notice("", "/帮助", now=NOW) == "/帮助"
    assert commands.append_piggyback_notice("g1", "", now=NOW) == ""
    other = commands.append_piggyback_notice("g9", "/帮助", now=NOW)  # 无紧急待办的群
    assert other == "/帮助"


def test_piggyback_commands_cover_readonly_queries_only(env):
    for name in ("帮助", "help", "课程列表", "FAQ列表", "比赛状态"):
        assert name in commands.PIGGYBACK_COMMANDS
    for write in ("报名", "课程添加", "FAQ添加", "发布", "征集创建"):
        assert write not in commands.PIGGYBACK_COMMANDS


# --------------------------------------------------------------- 调度任务

def _bot(api):
    client = QQBotClient("offline-app", "offline-secret")
    client._client = SimpleNamespace(api=api)
    return main.ACMBot(client)


def test_daily_digest_job_skips_quota_exhausted_group(env, caplog):
    _collection("g1", deadline="2026-11-11 21:00")
    _collection("g2", deadline="2026-11-11 21:00")

    async def post_group_message(*args, **kwargs):
        target = kwargs.get("group_openid") or kwargs.get("group_id") or (args[0] if args else "")
        if str(target) == "g1":
            raise ServerError({"code": 11253, "message": "主动消息配额已耗尽"})
        return {"id": "sent"}

    api = SimpleNamespace(post_group_message=AsyncMock(side_effect=post_group_message))
    bot = _bot(api)

    with caplog.at_level("WARNING"):
        sent = asyncio.run(bot._daily_digest_job(now=NOW))

    assert sent == 1  # g1 配额耗尽被跳过，g2 正常推送
    assert db.get_group_notification_block("qqbot", "g1")
    assert "配额已耗尽" in caplog.text
    assert not db.get_group_notification_block("qqbot", "g2")


def test_daily_digest_job_sends_nothing_when_no_todo(env):
    api = SimpleNamespace(post_group_message=AsyncMock(return_value={"id": "sent"}))
    bot = _bot(api)

    assert asyncio.run(bot._daily_digest_job(now=NOW)) == 0
    api.post_group_message.assert_not_awaited()


def test_daily_digest_job_respects_switches(env, monkeypatch):
    _collection("g1", deadline="2026-11-11 21:00")
    api = SimpleNamespace(post_group_message=AsyncMock(return_value={"id": "sent"}))
    bot = _bot(api)

    monkeypatch.setattr(config, "DAILY_DIGEST_ENABLED", False)
    assert asyncio.run(bot._daily_digest_job(now=NOW)) == 0
    monkeypatch.setattr(config, "DAILY_DIGEST_ENABLED", True)
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", False)
    assert asyncio.run(bot._daily_digest_job(now=NOW)) == 0
    api.post_group_message.assert_not_awaited()
