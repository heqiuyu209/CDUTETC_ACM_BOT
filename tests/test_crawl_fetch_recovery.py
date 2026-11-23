"""真实 HTTP 解码链与错误 JSON 降级的离线回归。"""

import asyncio
import json
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

import pytest

from cdutetc import crawler, fetcher

NOW = datetime(2026, 11, 23, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
HTML = ('<li><a href="/acm/contest/12345">离线牛客赛</a>'
        '<em>2026-11-24 19:00-21:00</em></li>')


async def empty_cf(url):
    return {"status": "OK", "result": []}


@pytest.mark.parametrize("payload", [
    {"code": 500, "msg": "offline error", "data": []},
    {"code": 0, "data": None},
    {"unexpected": []},
    {"code": 0, "data": [{"garbage": True}]},
])
async def test_error_or_unrecognized_calendar_json_uses_page(payload):
    calls = []

    async def nc(url):
        calls.append(url)
        return HTML if url == crawler.NOWCODER_CONTEST_PAGE else json.dumps(payload)

    result = await crawler.fetch_contests(NOW, codeforces_fetch=empty_cf, nowcoder_fetch=nc)
    assert len(result["nowcoder"]) == 1
    assert result["nowcoder"][0]["start"] == "2026-11-24 19:00"
    assert result["errors"] == []
    assert calls[-1] == crawler.NOWCODER_CONTEST_PAGE


@pytest.mark.parametrize("payload", [
    {"code": 0, "data": []}, {"data": {"contests": []}}, [],
])
async def test_valid_empty_calendar_does_not_fall_back(payload):
    async def nc(url):
        assert url != crawler.NOWCODER_CONTEST_PAGE
        return payload

    result = await crawler.fetch_contests(NOW, codeforces_fetch=empty_cf, nowcoder_fetch=nc)
    assert result == {"codeforces": [], "nowcoder": [], "errors": []}


async def test_partial_calendar_failure_preserves_good_month_and_reports_unusable_page():
    async def nc(url):
        if "month=2026-11" in url:
            return {"code": 0, "data": [{"contestId": 12345, "name": "离线牛客赛",
                                        "startTime": "2026-11-24 19:00"}]}
        if "month=2026-12" in url:
            return {"code": 500, "data": []}
        return "<html><p>offline challenge page</p></html>"

    result = await crawler.fetch_contests(NOW, codeforces_fetch=empty_cf, nowcoder_fetch=nc)
    assert [item["name"] for item in result["nowcoder"]] == ["离线牛客赛"]
    assert len(result["errors"]) == 1
    assert result["errors"][0]["platform"] == "牛客"
    assert "code=500" in result["errors"][0]["error"]


async def test_partial_calendar_failure_is_visible_even_when_page_returns_known_contest():
    async def nc(url):
        if "month=2026-11" in url:
            return {"code": 0, "data": [{"contestId": 12345, "name": "离线牛客赛",
                                        "startTime": "2026-11-24 19:00"}]}
        if "month=2026-12" in url:
            raise OSError("offline December unavailable")
        return HTML

    result = await crawler.fetch_contests(NOW, codeforces_fetch=empty_cf, nowcoder_fetch=nc)
    assert len(result["nowcoder"]) == 1
    assert result["errors"][0]["platform"] == "牛客"
    assert "month=2026-12" in result["errors"][0]["error"]


async def test_valid_calendar_items_outside_window_are_normal_empty_schedule():
    async def nc(url):
        assert url != crawler.NOWCODER_CONTEST_PAGE
        return {"code": 0, "data": [{"contestId": 12345, "name": "过去的比赛",
                                    "startTime": "2026-10-01 19:00"}]}

    result = await crawler.fetch_contests(NOW, codeforces_fetch=empty_cf, nowcoder_fetch=nc)
    assert result == {"codeforces": [], "nowcoder": [], "errors": []}


@pytest.mark.parametrize("payload", [
    {"status": "FAILED", "comment": "offline limit"},
    {"status": "OK", "result": None}, [],
])
async def test_codeforces_failed_or_unrecognized_json_is_source_error(payload):
    async def cf(url):
        return payload

    async def nc(url):
        return {"code": 0, "data": []}

    result = await crawler.fetch_contests(NOW, codeforces_fetch=cf, nowcoder_fetch=nc)
    assert result["codeforces"] == []
    assert result["errors"][0]["platform"] == "Codeforces"


async def test_production_page_fetch_keeps_links_after_calendar_error(monkeypatch):
    calls = []

    async def request(url):
        calls.append((url, fetcher._extra_headers.get()))
        body = HTML if url == crawler.NOWCODER_CONTEST_PAGE else '{"code":500,"data":[]}'
        ctype = "text/html" if url == crawler.NOWCODER_CONTEST_PAGE else "application/json"
        return 200, {"content-type": ctype + "; charset=utf-8"}, body.encode()

    monkeypatch.setattr(fetcher, "_request_once", request)
    result = await crawler.fetch_contests(NOW, codeforces_fetch=empty_cf)
    assert len(result["nowcoder"]) == 1
    assert result["nowcoder"][0]["url"] == "https://ac.nowcoder.com/acm/contest/12345"
    assert result["errors"] == []
    assert all(("Referer", crawler.NOWCODER_REFERER) in headers for _, headers in calls)
    # 文档模式与附加头不泄漏到普通正文抓取。
    assert await fetcher.fetch_text(crawler.NOWCODER_CONTEST_PAGE) == "离线牛客赛\n2026-11-24 19:00-21:00"
    assert calls[-1][1] == ()


@pytest.mark.parametrize("escape_times", [1, 2])
def test_official_page_data_json_uses_contest_time_instead_of_registration(escape_times):
    start = datetime(2026, 11, 27, 19, tzinfo=NOW.tzinfo)
    end = datetime(2026, 11, 27, 21, 30, tzinfo=NOW.tzinfo)
    raw = json.dumps({"contestId": 12345, "contestName": "离线牛客赛",
                      "contestStartTime": int(start.timestamp() * 1000),
                      "contestEndTime": int(end.timestamp() * 1000)})
    for _ in range(escape_times):
        raw = escape(raw, quote=True)
    page = (f'<div class="platform-item" data-json="{raw}"><div><h4>'
            '<a href="/acm/contest/12345">离线牛客赛</a></h4><ul>'
            '<li>报名时间：2026-11-03 10:00 至 2026-11-27 21:30</li>'
            '<li>比赛时间：2026-11-27 19:00 至 2026-11-27 21:30</li></ul></div></div>')
    item = crawler.parse_nowcoder(page, NOW)[0]
    assert item["start"] == "2026-11-27 19:00"
    assert item["end"] == "2026-11-27 21:30"
    assert item["duration_minutes"] == 150


async def test_document_fetch_redirects_and_keeps_existing_validation(monkeypatch):
    calls = []

    async def request(url):
        calls.append(url)
        if len(calls) == 1:
            return 302, {"location": "/contests"}, b""
        return 200, {"content-type": "text/html; charset=gb18030"}, HTML.encode("gb18030")

    monkeypatch.setattr(fetcher, "_request_once", request)
    assert await fetcher.fetch_document("https://contest.example.com/") == HTML
    assert calls == ["https://contest.example.com/", "https://contest.example.com/contests"]
    with pytest.raises(fetcher.FetchError, match="http/https"):
        await fetcher.fetch_document("file:///offline")
    with pytest.raises(fetcher.FetchError, match="非法请求头"):
        await fetcher.fetch_document("https://contest.example.com/", {"Referer": "x\r\nInjected: y"})


async def test_document_and_visible_fetches_are_isolated_between_tasks(monkeypatch):
    async def request(url):
        await asyncio.sleep(0)
        return 200, {"content-type": "text/html; charset=utf-8"}, HTML.encode()

    monkeypatch.setattr(fetcher, "_request_once", request)
    raw, visible = await asyncio.gather(
        fetcher.fetch_document("https://contest.example.com/"),
        fetcher.fetch_text("https://contest.example.com/"),
    )
    assert raw == HTML
    assert visible == "离线牛客赛\n2026-11-24 19:00-21:00"
