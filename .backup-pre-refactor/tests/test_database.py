"""数据库按群隔离 / 迁移 / 去重离线测试（内存 SQLite）。"""

import sqlite3

import pytest

import database


@pytest.fixture
def db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    database.init_db()
    yield conn
    conn.close()


def _contest(name="校赛", **overrides):
    info = {
        "name": name,
        "reg_deadline": "2026-11-01 10:30",
        "contest_time": "2026-11-05 09:00",
        "mode": "线上",
        "location": "机房",
        "source_link": "https://a.com",
        "raw_note": "说明",
    }
    info.update(overrides)
    return info


def test_save_and_list_by_group(db):
    id1 = database.save_contest("g1", _contest("甲"))
    database.save_contest("g2", _contest("乙"))

    g1 = database.list_contests("g1")
    g2 = database.list_contests("g2")
    assert len(g1) == 1 and g1[0]["name"] == "甲" and g1[0]["group_id"] == "g1"
    assert len(g2) == 1 and g2[0]["name"] == "乙"

    all_rows = database.list_contests()
    assert len(all_rows) == 2


def test_list_recent_limit(db):
    for i in range(8):
        database.save_contest("g1", _contest(f"比赛{i}"))
    assert len(database.list_recent_contests("g1", limit=3)) == 3


def test_delete_only_own_group(db):
    cid = database.save_contest("g1", _contest("甲"))
    assert database.delete_contest("g2", cid) is False
    assert len(database.list_contests("g1")) == 1
    assert database.delete_contest("g1", cid) is True
    assert len(database.list_contests("g1")) == 0


def test_save_contest_rejects_non_str(db):
    with pytest.raises(ValueError):
        database.save_contest("g1", {"name": 123})


def test_migrate_v1_adds_group_id(monkeypatch):
    """旧表无 group_id 列时，init_db 自动补列且旧数据保留、归属为空。"""
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    conn.execute(
        "CREATE TABLE contests ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL DEFAULT '',"
        " reg_deadline TEXT NOT NULL DEFAULT '', contest_time TEXT NOT NULL DEFAULT '',"
        " mode TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '',"
        " source_link TEXT NOT NULL DEFAULT '', raw_note TEXT NOT NULL DEFAULT '',"
        " created_at TEXT NOT NULL DEFAULT '')"
    )
    conn.execute(
        "INSERT INTO contests (name) VALUES ('旧记录')"
    )
    database.init_db()

    rows = conn.execute(
        "SELECT group_id, name FROM contests WHERE name='旧记录'"
    ).fetchall()
    assert rows == [("", "旧记录")]  # 旧记录 group_id 为空 = 隔离保留


def test_reminder_dedup(db):
    cid = database.save_contest("g1", _contest("甲"))
    key = ("g1", cid, "reg_deadline", "2026-11-01 10:30")
    assert database.has_reminded(*key) is False
    assert database.mark_reminded(*key) is True
    assert database.has_reminded(*key) is True
    # 重复标记：唯一键冲突，返回 False
    assert database.mark_reminded(*key) is False
