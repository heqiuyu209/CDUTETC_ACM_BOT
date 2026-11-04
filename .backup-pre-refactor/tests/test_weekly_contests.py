"""自动周赛日期、持久化幂等及事务并发的离线回归测试。"""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

import config
import database
import main


BEIJING = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def memory_db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    database.init_db()
    yield conn
    conn.close()


@pytest.fixture
async def bot(memory_db, monkeypatch):
    monkeypatch.setattr(config, "WEEKLY_CONTEST_ENABLED", True)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_WEEKDAY", 6)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_START", "19:00")
    monkeypatch.setattr(config, "WEEKLY_CONTEST_END", "21:00")
    monkeypatch.setattr(config, "WEEKLY_CONTEST_TEMPLATE", "ACM 周赛（本周）")
    client = main.ACMBot(SimpleNamespace(send_group_msg=AsyncMock(return_value=True)))
    monkeypatch.setattr(client, "_send_group_log_only", AsyncMock(return_value=False))
    return client


def _now(day=1):
    return datetime(2026, 11, day, 18, 0, tzinfo=BEIJING)


def _info(name="ACM 周赛（本周）"):
    return {
        "name": name,
        "official_time": "2026-11-01 19:00",
        "summary": "周赛时间窗：2026-11-01 19:00 至 2026-11-01 21:00",
    }


async def test_weekly_generation_stores_concrete_beijing_window(bot, memory_db):
    await bot._weekly_contest_job(now=_now())

    competitions = database.list_competitions(status="confirmed")
    assert len(competitions) == 1
    competition = competitions[0]
    assert competition["name"] == "ACM 周赛（本周）"
    assert competition["official_time"] == "2026-11-01 19:00"
    assert "2026-11-01 21:00" in competition["summary"]
    assert competition["official_reg_deadline"] == ""
    assert len(database.list_competition_versions(competition["id"])) == 1
    assert memory_db.execute(
        "SELECT week_start, competition_id FROM weekly_contest_runs"
    ).fetchall() == [("2026-10-26", competition["id"])]
    bot._send_group_log_only.assert_not_awaited()


async def test_same_week_remains_idempotent_after_restart_and_amendment(bot, memory_db, monkeypatch):
    await bot._weekly_contest_job(now=_now())
    competition = database.list_competitions()[0]
    assert database.amend_competition(competition["id"], {
        "name": "老师修正的周赛标题",
        "official_time": "2026-11-02 20:00",
    }, operator="admin")
    monkeypatch.setattr(config, "WEEKLY_CONTEST_TEMPLATE", "新的周赛模板")
    monkeypatch.setattr(config, "WEEKLY_CONTEST_START", "20:00")
    monkeypatch.setattr(config, "WEEKLY_CONTEST_END", "22:00")

    await bot._weekly_contest_job(now=_now())
    restarted = main.ACMBot(SimpleNamespace(send_group_msg=AsyncMock(return_value=True)))
    await restarted._weekly_contest_job(now=_now())

    competitions = database.list_competitions()
    assert len(competitions) == 1
    assert competitions[0]["id"] == competition["id"]
    assert competitions[0]["name"] == "老师修正的周赛标题"
    assert competitions[0]["official_time"] == "2026-11-02 20:00"
    assert memory_db.execute("SELECT COUNT(*) FROM weekly_contest_runs").fetchone()[0] == 1
    assert len(database.list_competition_versions(competition["id"])) == 2


async def test_next_week_creates_new_record_with_same_template(bot, memory_db):
    await bot._weekly_contest_job(now=_now(1))
    await bot._weekly_contest_job(now=_now(8))

    competitions = database.list_competitions()
    assert len(competitions) == 2
    assert {c["name"] for c in competitions} == {"ACM 周赛（本周）"}
    assert {c["official_time"] for c in competitions} == {
        "2026-11-01 19:00", "2026-11-08 19:00",
    }
    assert [r[0] for r in memory_db.execute(
        "SELECT week_start FROM weekly_contest_runs ORDER BY week_start"
    ).fetchall()] == ["2026-10-26", "2026-11-02"]


async def test_utc_clock_crosses_into_beijing_week_and_honors_weekday(bot, memory_db, monkeypatch):
    monkeypatch.setattr(config, "WEEKLY_CONTEST_WEEKDAY", 2)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_START", "18:30")
    monkeypatch.setattr(config, "WEEKLY_CONTEST_END", "20:30")
    # UTC 周日 16:30 已是北京时间周一 00:30，应使用新一周的周三。
    await bot._weekly_contest_job(now=datetime(2026, 11, 1, 16, 30, tzinfo=timezone.utc))

    competition = database.list_competitions()[0]
    assert competition["official_time"] == "2026-11-04 18:30"
    assert "2026-11-04 20:30" in competition["summary"]
    assert memory_db.execute("SELECT week_start FROM weekly_contest_runs").fetchone()[0] == "2026-11-02"


async def test_disabled_weekly_generation_does_not_create_records(bot, memory_db, monkeypatch):
    monkeypatch.setattr(config, "WEEKLY_CONTEST_ENABLED", False)
    await bot._weekly_contest_job(now=_now())
    assert database.list_competitions() == []
    assert memory_db.execute("SELECT COUNT(*) FROM weekly_contest_runs").fetchone()[0] == 0


@pytest.mark.parametrize("setting, value", [
    ("WEEKLY_CONTEST_START", "25:00"),
    ("WEEKLY_CONTEST_END", "18:00"),
    ("WEEKLY_CONTEST_WEEKDAY", 7),
])
async def test_invalid_weekly_configuration_does_not_create_records(bot, memory_db, monkeypatch, setting, value):
    monkeypatch.setattr(config, setting, value)
    await bot._weekly_contest_job(now=_now())
    assert database.list_competitions() == []
    assert memory_db.execute("SELECT COUNT(*) FROM weekly_contest_runs").fetchone()[0] == 0


def test_schema_upgrade_adds_weekly_runs_and_preserves_existing_records(memory_db):
    # 此内存库移除新表后模拟旧版已部署数据库，保留其业务数据。
    memory_db.execute("DROP TABLE weekly_contest_runs")
    legacy_id = database.save_contest("g1", {"name": "历史群比赛"})
    public_id = database.create_competition({"name": "历史公共比赛"}, status="confirmed")
    versions = database.list_competition_versions(public_id)

    database.init_db()
    database.init_db()

    assert database.list_contests("g1")[0]["id"] == legacy_id
    assert database.get_competition(public_id)["name"] == "历史公共比赛"
    assert database.list_competition_versions(public_id) == versions
    assert memory_db.execute("SELECT COUNT(*) FROM weekly_contest_runs").fetchone()[0] == 0
    cid, created = database.create_weekly_competition("2026-10-26", _info())
    assert created is True
    assert database.get_competition(cid)["official_time"] == "2026-11-01 19:00"


def test_weekly_failure_rolls_back_competition_version_and_marker_then_retries(memory_db, monkeypatch):
    real_add_version = database._add_version

    def fail_after_version(*args, **kwargs):
        real_add_version(*args, **kwargs)
        raise RuntimeError("模拟版本写入后失败")

    with monkeypatch.context() as failure:
        failure.setattr(database, "_add_version", fail_after_version)
        with pytest.raises(RuntimeError, match="模拟版本写入后失败"):
            database.create_weekly_competition("2026-10-26", _info())

    assert memory_db.execute("SELECT COUNT(*) FROM competitions").fetchone()[0] == 0
    assert memory_db.execute("SELECT COUNT(*) FROM competition_versions").fetchone()[0] == 0
    assert memory_db.execute("SELECT COUNT(*) FROM weekly_contest_runs").fetchone()[0] == 0

    cid, created = database.create_weekly_competition("2026-10-26", _info())
    assert created is True
    assert database.create_weekly_competition("2026-10-26", _info("不同标题")) == (cid, False)
    assert len(database.list_competition_versions(cid)) == 1


def test_independent_database_connections_create_one_weekly_record(tmp_path, monkeypatch):
    path = str(tmp_path / "weekly.db")
    monkeypatch.setattr(database, "DB_PATH", path)
    database.init_db()
    barrier = threading.Barrier(4)

    def create_weekly(_):
        barrier.wait(timeout=5)
        return database.create_weekly_competition("2026-10-26", _info())

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(create_weekly, range(4)))

    assert len({cid for cid, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM competitions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM competition_versions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM weekly_contest_runs").fetchone()[0] == 1
