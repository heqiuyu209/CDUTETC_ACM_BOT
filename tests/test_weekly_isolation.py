"""周赛显式群范围在查询、计划生成、旧队列和实际投递中的一致性。"""

from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from cdutetc import config, database as db, reminder


NOW = datetime(2026, 11, 8, 18, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "scope.sqlite3"))
    monkeypatch.setattr(db, "_delivery_now", lambda value=None: value or NOW)
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"team", "new"})
    monkeypatch.setattr(config, "WEEKLY_CONTEST_GROUPS", set())
    monkeypatch.setattr(config, "CONTEST_REMIND_OFFSETS_MINUTES", (30, 15))
    db.init_db()


def weekly():
    return db.create_weekly_competition("2026-11-02", {
        "name": "集训队周赛", "official_time": "2026-11-08 19:00",
    })[0]


def test_unconfigured_weekly_never_becomes_global_when_new_group_is_added(store, monkeypatch):
    cid = weekly()
    assert db.get_competition_target_groups(cid) == set()
    assert db.list_question_contests("team") == []
    assert db.list_question_contests("new") == []
    assert db.list_deliveries("new") == db.list_deliveries("team") == []
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"team", "new", "another"})
    assert db.get_competition_target_groups(cid) == set()
    assert db.list_question_contests("another") == []


async def test_scoped_weekly_only_queries_and_sends_to_team(store, monkeypatch):
    monkeypatch.setattr(config, "WEEKLY_CONTEST_GROUPS", {"team"})
    cid = weekly()
    assert db.get_competition_target_groups(cid) == {"team"}
    assert [c["name"] for c in db.list_question_contests("team")] == ["集训队周赛"]
    assert db.list_question_contests("new") == []
    assert db.list_deliveries("new") == []
    sender = AsyncMock(return_value=True)
    assert await reminder.check_contest_start_reminders(sender, now=NOW.replace(minute=30)) == 1
    assert sender.call_args.args[0] == "team"


async def test_old_broadcast_queue_is_cancelled_before_send(store):
    cid = weekly()
    db.ensure_delivery("new", "competition", cid, "contest_start", "2026-11-08 19:00", 0.5)
    with db._connect() as conn:
        conn.execute("UPDATE reminder_deliveries SET scheduled_at='2026-11-08 18:30:00', expires_at='2026-11-08 18:32:00'")
    sender = AsyncMock(return_value=True)
    assert await reminder.check_contest_start_reminders(sender, now=NOW.replace(minute=30)) == 0
    sender.assert_not_awaited()
    assert db.list_deliveries("new")[0]["status"] == "cancelled"


def test_public_contests_still_share_but_weekly_empty_scope_stays_empty_after_amendment(store):
    cid = weekly()
    public = db.create_competition({"name": "公共校赛", "official_time": "2026-11-08 19:00"}, status="confirmed")
    assert db.get_competition_target_groups(public) == {"team", "new"}
    db.amend_competition(cid, {"name": "更名后的周赛"})
    assert [c["name"] for c in db.list_question_contests("new")] == ["公共校赛"]
    assert not any(d["obj_id"] == cid for d in db.list_deliveries("new"))


def test_clearing_weekly_scope_cancels_future_nodes_without_making_it_public(store, monkeypatch):
    monkeypatch.setattr(config, "WEEKLY_CONTEST_GROUPS", {"team"})
    cid = weekly()
    db.set_competition_notify_groups(cid, set())
    assert db.get_competition_target_groups(cid) == set()
    assert db.list_question_contests("team") == []
    assert all(d["status"] == "cancelled" for d in db.list_deliveries("team"))
