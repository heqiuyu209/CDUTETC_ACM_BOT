"""规格 F：Codeforces / 牛客赛程抓取与自动建档。

所有网络入口都用注入的假 fetcher 顶替，并对真实抓取函数打哨兵：
一旦有测试漏配注入而走到真实 HTTP，哨兵会立刻让用例失败，不会真的联网。
"""

import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from cdutetc import config, crawler, database as db

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 12, 10, 0, tzinfo=TZ)  # 周四上午
GID = "123456"


@pytest.fixture()
def no_network(monkeypatch):
    """真实抓取通道一律炸掉：任何联网尝试都会直接暴露成测试失败。"""
    async def _boom(*args, **kwargs):
        raise AssertionError("测试禁止真实联网，请注入假 fetcher")

    monkeypatch.setattr(crawler, "_http_get_text", _boom)
    return _boom


@pytest.fixture()
def env(tmp_path, monkeypatch, no_network):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "crawl.sqlite3"))
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {GID}, raising=False)
    monkeypatch.setattr(config, "AUTO_CONTEST_WINDOW_DAYS", 14, raising=False)
    monkeypatch.setattr(config, "AUTO_CONTEST_NOTIFY_GROUPS", frozenset(), raising=False)
    db.init_db()
    return GID


def _cf_payload(*contests) -> dict:
    return {"status": "OK", "result": list(contests)}


def _cf_contest(cid: int, name: str, start_epoch: int, duration: int = 7200) -> dict:
    return {"id": cid, "name": name, "startTimeSeconds": start_epoch,
            "durationSeconds": duration, "phase": "BEFORE"}


def run(coro):
    return asyncio.run(coro)


def test_parse_codeforces_keeps_only_contests_in_window():
    """窗口内且未开始的比赛入列，超窗口的比赛丢弃。"""
    in_window = int(datetime(2026, 11, 14, 22, 35, tzinfo=TZ).timestamp())
    far_away = int(datetime(2027, 3, 1, 22, 35, tzinfo=TZ).timestamp())
    items = crawler.parse_codeforces(
        _cf_payload(_cf_contest(1, "Codeforces Round 1000", in_window),
                    _cf_contest(2, "Far Future Round", far_away)),
        NOW, window_days=14)
    assert [item["name"] for item in items] == ["Codeforces Round 1000"]
    assert items[0]["platform"] == "Codeforces"
    assert items[0]["start"] == "2026-11-14 22:35"
    assert items[0]["url"].endswith("/contest/1")


def test_parse_codeforces_ignores_failed_status():
    """接口 status 非 OK 时不产出任何比赛，也不抛异常。"""
    assert crawler.parse_codeforces({"status": "FAILED", "comment": "limit"}, NOW) == []


def test_nowcoder_json_accepts_name_aliases():
    """牛客接口字段名不稳定：contestName / contestTitle / contest_name 都要认。"""
    payload = {"data": [
        {"contestId": 7001, "contestName": "牛客练习赛 130",
         "startTime": "2026-11-14 19:00:00", "endTime": "2026-11-14 21:00:00"},
        {"contestId": 7002, "contestTitle": "小白月赛 100",
         "startTime": "2026-11-15 19:00:00", "endTime": "2026-11-15 21:00:00"},
        {"contestId": 7003, "contest_name": "寒假训练赛",
         "startTime": "2026-11-16 19:00:00", "endTime": "2026-11-16 21:30:00"},
    ]}
    names = [item["name"] for item in crawler.parse_nowcoder(
        json.dumps(payload, ensure_ascii=False), NOW)]
    assert names == ["牛客练习赛 130", "小白月赛 100", "寒假训练赛"]


def test_nowcoder_json_computes_end_and_duration():
    payload = {"data": [
        {"contestId": 7100, "contestName": "练习赛 131",
         "startTime": "2026-11-13 19:00:00", "endTime": "2026-11-13 21:30:00"}]}
    item = crawler.parse_nowcoder(json.dumps(payload, ensure_ascii=False), NOW)[0]
    assert item["start"] == "2026-11-13 19:00"
    assert item["end"] == "2026-11-13 21:30"
    assert item["duration_minutes"] == 150
    assert item["url"].endswith("/7100")


def test_nowcoder_html_fills_end_and_duration_from_range():
    """HTML 列表项里标题后的「19:00-21:00」要补出结束时间与时长。"""
    html = ('<li><a href="/acm/contest/8001">2026 牛客练习赛 132</a>'
            '<em>2026-11-14 19:00-21:00</em></li>')
    item = crawler.parse_nowcoder(html, NOW)[0]
    assert item["start"] == "2026-11-14 19:00"
    assert item["end"] == "2026-11-14 21:00"
    assert item["duration_minutes"] == 120


def test_nowcoder_html_cross_day_range_ends_next_day():
    """跨零点时段（23:00-01:00）结束时间落到次日，时长仍按区间算。"""
    html = ('<li><a href="/acm/contest/8002">牛客夜场赛</a>'
            '<em>2026-11-15 23:00-01:00</em></li>')
    item = crawler.parse_nowcoder(html, NOW)[0]
    assert item["start"] == "2026-11-15 23:00"
    assert item["end"] == "2026-11-16 01:00"
    assert item["duration_minutes"] == 120


def test_nowcoder_html_skips_entry_without_any_time():
    """没有时间的条目不入列，绝不编造开始时间。"""
    html = ('<li><a href="/acm/contest/8003">时间待定赛</a></li>'
            '<li><a href="/acm/contest/8004">正常赛</a><em>2026-11-14 19:00-21:00</em></li>')
    names = [item["name"] for item in crawler.parse_nowcoder(html, NOW)]
    assert names == ["正常赛"]


def test_parse_nowcoder_handles_blank_input():
    assert crawler.parse_nowcoder("", NOW) == []
    assert crawler.parse_nowcoder("   ", NOW) == []


def test_fetch_contests_isolates_single_source_failure(env):
    """牛客挂掉只记一条 error，Codeforces 结果照常返回。"""
    start_epoch = int(datetime(2026, 11, 14, 22, 35, tzinfo=TZ).timestamp())

    async def cf_fetch(url):
        return _cf_payload(_cf_contest(3, "Codeforces Round 1001", start_epoch))

    async def nc_fetch(url):
        raise TimeoutError("nowcoder timeout")

    result = run(crawler.fetch_contests(NOW, codeforces_fetch=cf_fetch, nowcoder_fetch=nc_fetch))
    assert [item["name"] for item in result["codeforces"]] == ["Codeforces Round 1001"]
    assert result["nowcoder"] == []
    assert result["errors"][0]["platform"] == "牛客"


def test_dedupe_contest_items_keeps_first_occurrence():
    items = [
        {"platform": "牛客", "name": "练习赛 133", "start": "2026-11-14 19:00"},
        {"platform": "牛客", "name": "练习赛 133", "start": "2026-11-14 19:00"},
        {"platform": "Codeforces", "name": "练习赛 133", "start": "2026-11-14 19:00"},
        {"platform": "牛客", "name": "", "start": "2026-11-14 19:00"},
    ]
    assert len(crawler.dedupe_contest_items(items)) == 2


def test_run_contest_fetch_disabled_by_default(env, monkeypatch):
    """默认关闭时直接返回 disabled，不发起任何抓取。"""
    monkeypatch.setattr(config, "AUTO_CONTEST_CRAWL_ENABLED", False, raising=False)
    calls: list[str] = []

    async def cf_fetch(url):
        calls.append(url)
        return _cf_payload()

    result = run(crawler.run_contest_fetch(NOW, fetchers={"codeforces_fetch": cf_fetch}))
    assert result[0]["kind"] == "disabled"
    assert calls == []


def test_run_contest_fetch_creates_competition_with_injected_fetcher(env, monkeypatch):
    """开启后抓到的新比赛建档并挂上通知群；重复抓取标记为 exists。"""
    monkeypatch.setattr(config, "AUTO_CONTEST_CRAWL_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "AUTO_CONTEST_AUTO_CONFIRM", True, raising=False)
    start_epoch = int(datetime(2026, 11, 20, 22, 35, tzinfo=TZ).timestamp())

    async def cf_fetch(url):
        return _cf_payload(_cf_contest(4, "Codeforces Round 1002", start_epoch))

    async def nc_fetch(url):
        return ('<li><a href="/acm/contest/8100">牛客练习赛 134</a>'
                '<em>2026-11-21 19:00-21:00</em></li>')

    fetchers = {"codeforces_fetch": cf_fetch, "nowcoder_fetch": nc_fetch}
    first = run(crawler.run_contest_fetch(NOW, fetchers=fetchers))
    created = [item for item in first if item["kind"] == "created"]
    assert {item["platform"] for item in created} == {"Codeforces", "牛客"}
    assert len(db.list_competitions()) == 2

    second = run(crawler.run_contest_fetch(NOW, fetchers=fetchers))
    assert {item["kind"] for item in second} == {"exists"}
    assert len(db.list_competitions()) == 2
