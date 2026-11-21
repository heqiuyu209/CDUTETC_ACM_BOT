"""平台赛事 ID 去重：标题变化、跨批抓取及手工字段/提醒范围保留。"""

import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from cdutetc import config, crawler, database as db, llm


TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 21, 8, 0, tzinfo=TZ)
START = datetime(2026, 11, 21, 22, 35, tzinfo=TZ)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "crawl_identity.sqlite3"))
    monkeypatch.setattr(db, "_delivery_now", lambda value=None: value or NOW)
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "AUTO_CONTEST_CRAWL_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_CONTEST_AUTO_CONFIRM", True)
    monkeypatch.setattr(config, "AUTO_CONTEST_WINDOW_DAYS", 14)
    monkeypatch.setattr(config, "AUTO_CONTEST_NOTIFY_GROUPS", {"g2"})
    monkeypatch.setattr(config, "WEEKLY_CONTEST_GROUPS", set())
    model = AsyncMock(side_effect=AssertionError("结构化赛程去重不调用模型"))
    monkeypatch.setattr(llm, "_chat_once", model)
    monkeypatch.setattr(llm, "extract_contest", model)
    db.init_db()
    return model


def _cf(cid, name, start=START):
    return {"id": cid, "name": name, "startTimeSeconds": int(start.timestamp()),
            "durationSeconds": 7200, "phase": "BEFORE"}


def _run(*contests):
    async def cf_fetch(_url):
        return {"status": "OK", "result": list(contests)}

    async def nc_fetch(_url):
        return json.dumps({"data": []})

    return asyncio.run(crawler.run_contest_fetch(NOW, fetchers={
        "codeforces_fetch": cf_fetch, "nowcoder_fetch": nc_fetch,
    }))


def _item(url, name="比赛名称", start="2026-11-21 22:35", platform="Codeforces"):
    return {"platform": platform, "name": name, "start": start, "url": url}


def test_codeforces_batch_dedupes_changed_round_titles_by_contest_id():
    parsed = crawler.parse_codeforces({"status": "OK", "result": [
        _cf(2288, "Codeforces Round 1180"),
        _cf(2288, "Codeforces Round 1181 (Div. 2)"),
        _cf(2287, "Codeforces Round 1179"),
        _cf(2287, "Codeforces Round 1180 (Div. 3)"),
    ]}, NOW)
    assert len(parsed) == 2
    assert {item["url"]: item["name"] for item in parsed} == {
        "https://codeforces.com/contest/2288": "Codeforces Round 1181 (Div. 2)",
        "https://codeforces.com/contest/2287": "Codeforces Round 1180 (Div. 3)",
    }


def test_same_platform_id_batch_does_not_use_title_or_start_as_identity():
    first = _item("https://codeforces.com/contest/2288", name="旧标题")
    later = _item("http://www.codeforces.com/contest/2288?locale=zh", name="更完整的新比赛标题",
                  start="2026-11-22 22:35")
    assert crawler.dedupe_contest_items([first, later]) == [later]


def test_different_platform_ids_and_codeforces_gym_namespace_stay_distinct():
    items = [
        _item("https://codeforces.com/contest/2288"),
        _item("https://codeforces.com/contest/2287"),
        _item("https://codeforces.com/gym/2288"),
        _item("https://ac.nowcoder.com/acm/contest/2288", platform="牛客"),
    ]
    assert crawler.dedupe_contest_items(items) == items


def test_unknown_platform_keeps_original_name_time_rule():
    items = [
        _item("https://example.com/contest/2288", "旧标题"),
        _item("https://example.com/contest/2288", "新标题"),
        _item("https://example.com/contest/2288", "旧标题", "2026-11-22 22:35"),
        _item("https://example.com/other", "旧标题"),
    ]
    assert crawler.dedupe_contest_items(items) == items[:3]


def test_nowcoder_json_uses_real_link_id_and_more_complete_batch_title():
    parsed = crawler.parse_nowcoder(json.dumps({"data": [
        {"contestId": 1143041, "link": "/acm/contest/143041?from=calendar",
         "name": "小白月赛", "startTime": "2026-11-21 19:00:00"},
        {"contestId": 143041, "link": "https://ac.nowcoder.com/acm/contest/143041",
         "name": "牛客小白月赛 130", "startTime": "2026-11-21 19:00:00"},
    ]}, ensure_ascii=False), NOW)
    assert len(parsed) == 1
    assert parsed[0]["name"] == "牛客小白月赛 130"
    assert parsed[0]["url"] == "https://ac.nowcoder.com/acm/contest/143041"


def test_nowcoder_html_dedupes_after_reading_more_complete_title():
    html = ('<li><a href="/acm/contest/143041">小白月赛</a>'
            '<span>2026-11-21 19:00-21:00</span></li>'
            '<li><a href="/acm/contest/143041">牛客小白月赛 130</a>'
            '<span>2026-11-21 19:00-21:00</span></li>')
    parsed = crawler.parse_nowcoder(html, NOW)
    assert len(parsed) == 1 and parsed[0]["name"] == "牛客小白月赛 130"


def test_run_crawl_title_change_keeps_existing_manual_fields_scope_and_reminders(env):
    cid = db.create_competition({
        "name": "老师自定义赛事名称", "official_site": "http://codeforces.com/contest/2288?locale=zh",
        "official_time": "2026-11-21 21:00", "official_reg_deadline": "2026-11-21 20:00",
        "device_req": "带电脑", "summary": "老师手工说明",
    }, status="confirmed", operator="teacher")
    db.set_competition_notify_groups(cid, {"g1"})
    before = db.get_competition(cid)
    with db._connect() as conn:
        plans = conn.execute("SELECT * FROM reminder_deliveries ORDER BY id").fetchall()
        versions = conn.execute("SELECT * FROM competition_versions ORDER BY id").fetchall()
    result = _run(_cf(2288, "Codeforces Round 1181 (Div. 2)"))
    assert [item["kind"] for item in result] == ["exists"]
    assert len(db.list_competitions()) == 1
    assert db.get_competition(cid) == before
    assert db.get_competition_notify_groups(cid) == {"g1"}
    with db._connect() as conn:
        assert conn.execute("SELECT * FROM reminder_deliveries ORDER BY id").fetchall() == plans
        assert conn.execute("SELECT * FROM competition_versions ORDER BY id").fetchall() == versions
    env.assert_not_awaited()


def test_run_crawl_renamed_later_batch_does_not_create_another_record(env):
    first = _run(_cf(2288, "Codeforces Round 1180"))
    cid = next(item["competition_id"] for item in first if item["kind"] == "created")
    second = _run(_cf(2288, "Codeforces Round 1181 (Div. 2)",
                      datetime(2026, 11, 22, 22, 35, tzinfo=TZ)))
    assert [item["kind"] for item in second] == ["exists"]
    assert len(db.list_competitions()) == 1
    assert db.get_competition(cid)["name"] == "Codeforces Round 1180"
    env.assert_not_awaited()


def test_run_crawl_existing_draft_does_not_become_confirmed_or_gain_scope(env):
    cid = db.create_competition({"name": "待审旧标题", "official_time": "2026-11-21 21:00",
                                 "official_site": "https://codeforces.com/contest/2288"}, status="draft")
    before = db.get_competition(cid)
    assert [item["kind"] for item in _run(_cf(2288, "Codeforces Round 1181"))] == ["exists"]
    assert db.get_competition(cid) == before
    assert db.get_competition_notify_groups(cid) == set()
    env.assert_not_awaited()
