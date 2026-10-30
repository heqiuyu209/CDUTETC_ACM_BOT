"""完整正文 → 有界模型分段 → 待审/基线；网络和模型均使用受控传输。"""

import asyncio
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest

import crawler
import database as db
import fetcher
import llm


URL = "https://contest.example.com/news"
NOW = datetime(2026, 11, 1, 10, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def source(monkeypatch):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(db, "_connect", lambda: conn)
    monkeypatch.setattr(db, "now_str", lambda: "2026-11-01 08:00:00")
    monkeypatch.setattr(crawler, "AUTO_FETCH_ENABLED", True)
    monkeypatch.setattr(crawler, "FETCH_MIN_INTERVAL_MINUTES", 60)
    monkeypatch.setattr(fetcher.config, "FETCH_TEXT_LIMIT", 3000)
    monkeypatch.setattr(fetcher.config, "FETCH_ALLOWED_DOMAINS", set())
    monkeypatch.setattr(fetcher.config, "FETCH_MAX_BYTES", 1_000_000)
    monkeypatch.setattr(llm.config, "LLM_MAX_INPUT_CHARS", 6000)
    db.init_db()
    cid = db.create_published_competition(
        {"name": "离线校赛", "official_reg_deadline": "2026-11-10 23:59"}, URL, "原公告正文",
    )
    state = {"body": b""}

    async def request(url):
        assert url == URL
        return 200, {"content-type": "text/plain; charset=utf-8", "etag": '"updated"'}, state["body"]

    monkeypatch.setattr(fetcher, "_request_once", request)
    yield cid, state
    conn.close()


def model_transport(monkeypatch, model):
    original = httpx.AsyncClient
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        assert payload["response_format"] == {"type": "json_object"}
        chunk = payload["messages"][1]["content"]
        assert len(chunk) <= llm.config.LLM_MAX_INPUT_CHARS
        calls.append(chunk)
        result = model(chunk, len(calls))
        if isinstance(result, httpx.Response):
            return result
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)}}]})

    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    return calls


def extract_visible_fields(chunk, index):
    """只按模型实际收到的完整字段回显，尾部字段不能凭测试常量补齐。"""
    fields = {}
    for field, label in (("name", "比赛名称"), ("tracks", "赛道"), ("device_req", "设备要求")):
        match = re.search(label + r"：([^\n]+)\n", chunk)
        if match:
            fields[field] = match[1]
    match = re.search(r"报名截止：(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", chunk)
    if match:
        fields["reg_deadline"] = match[1]
    return fields


async def test_actual_fetch_and_model_cover_head_middle_tail_and_keep_nochange(source, monkeypatch):
    cid, state = source
    text = (
        "比赛名称：离线校赛\n" + "正文填充\n" * 700 + "赛道：个人赛和团体赛\n"
        + "导航填充\n" * 600 + "设备要求：自带编译环境\n报名截止：2026-11-20 23:59\n"
    )
    assert len(text) > 6000
    state["body"] = text.encode()
    calls = model_transport(monkeypatch, extract_visible_fields)

    results = await crawler.run_fetch(now=NOW)
    assert results[0]["kind"] == "pending_auto"
    assert len(calls) == 2
    assert "赛道：个人赛和团体赛\n" in calls[0]
    assert "设备要求：自带编译环境\n" in calls[1]
    assert "2026-11-20 23:59" in calls[1]
    pending = db.get_pending_update(cid)
    assert pending["fields"]["official_reg_deadline"] == "2026-11-20 23:59"
    assert pending["fields"]["tracks"] == "个人赛和团体赛"
    assert pending["fields"]["device_req"] == "自带编译环境"
    assert db.get_competition(cid)["official_reg_deadline"] == "2026-11-10 23:59"
    assert db.list_sources(cid)[0]["body_hash"] == hashlib.sha256(text.encode()).hexdigest()
    assert db.apply_pending_update(pending["id"], "admin")
    assert db.get_competition(cid)["official_reg_deadline"] == "2026-11-20 23:59"

    second = await crawler.run_fetch(now=NOW + timedelta(hours=1))
    assert second[0]["kind"] == "nochange" and len(calls) == 2


async def test_overlap_keeps_date_split_at_request_boundary_complete(monkeypatch):
    text = "比赛名称：离线校赛\n"
    text += "x" * (5990 - len(text))
    text += "\n报名截止：2026-11-20 23:59\n"
    calls = model_transport(monkeypatch, extract_visible_fields)
    info, error = await llm.extract_contest(text)
    assert error is None and info["reg_deadline"] == "2026-11-20 23:59"
    assert len(calls) == 2
    assert "2026-11-20 23:59" not in calls[0] and "2026-11-20 23:59" in calls[1]


@pytest.mark.parametrize("failure", ["http", "invalid_field", "conflict", "missing_name", "budget"])
async def test_incomplete_extraction_preserves_baseline_pending_and_retries(source, monkeypatch, failure):
    cid, state = source
    text = "比赛名称：离线校赛\n" + "导航填充\n" * 1400 + "报名截止：2026-11-20 23:59\n"
    if failure == "budget":
        text += "超长正文" * 12000
    state["body"] = text.encode()
    old_source = db.list_sources(cid)[0]
    old_comp = db.get_competition(cid)
    db.add_pending_update(cid, {"summary": "已有人工待审"}, source_id=old_source["id"])
    old_pending = db.get_pending_update(cid)
    fail = {"enabled": True}

    def model(chunk, index):
        if fail["enabled"]:
            if failure == "missing_name":
                return {"reg_deadline": "2026-11-20 23:59"} if index == 2 else {}
            if failure == "conflict":
                return {"name": "离线校赛", "reg_deadline": "2026-11-10 23:59"} if index == 1 else {
                    "official_reg_deadline": "2026-11-20 23:59",
                }
            if index == 2 and failure == "http":
                return httpx.Response(503)
            if index == 2 and failure == "invalid_field":
                return {"reg_deadline": "not-a-date"}
        return extract_visible_fields(chunk, index)

    calls = model_transport(monkeypatch, model)
    first = await crawler.run_fetch(now=NOW)
    assert first[0]["kind"] == "error"
    after = db.list_sources(cid)[0]
    assert after["body_hash"] == old_source["body_hash"]
    assert after["etag"] == old_source["etag"]
    assert after["last_status"] == "error" and after["last_error"]
    assert db.get_competition(cid) == old_comp
    assert db.get_pending_update(cid) == old_pending
    if failure == "budget":
        assert calls == [] and "超过完整提取预算" in after["last_error"]
        state["body"] = ("比赛名称：离线校赛\n" + "正文\n" * 2200 + "报名截止：2026-11-20 23:59\n").encode()
    elif failure == "conflict":
        assert "字段冲突：reg_deadline" in after["last_error"]
    else:
        assert len(calls) == 2
    prior_calls = len(calls)
    skipped = await crawler.run_fetch(now=NOW + timedelta(minutes=1))
    assert skipped[0]["kind"] == "skip" and len(calls) == prior_calls
    fail["enabled"] = False
    retry = await crawler.run_fetch(now=NOW + timedelta(hours=1))
    assert retry[0]["kind"] == "pending_auto" and len(calls) == prior_calls + 2
    assert db.get_pending_update(cid)["fields"]["official_reg_deadline"] == "2026-11-20 23:59"
    assert db.list_sources(cid)[0]["body_hash"] != old_source["body_hash"]


async def test_conflicting_aliases_in_same_chunk_are_not_silently_selected(monkeypatch):
    model_transport(monkeypatch, lambda chunk, index: {
        "name": "离线校赛", "reg_deadline": "2026-11-10 23:59",
        "official_reg_deadline": "2026-11-20 23:59",
    })
    info, error = await llm.extract_contest("离线校赛公告")
    assert info is None and "字段冲突：reg_deadline" in error


async def test_normalized_equal_aliases_and_nondate_fields_merge(monkeypatch):
    model_transport(monkeypatch, lambda chunk, index: {
        "name": "离线校赛", "reg_deadline": "2026-11-20 9:00", "tracks": "团体赛",
    } if index == 1 else {"official_reg_deadline": "2026/11/20 09:00", "device_req": "自带电脑"})
    info, error = await llm.extract_contest("x" * 7500)
    assert error is None and info["reg_deadline"] == "2026-11-20 09:00"
    assert info["tracks"] == "团体赛" and info["device_req"] == "自带电脑"


async def test_nondate_conflict_requires_review(monkeypatch):
    model_transport(monkeypatch, lambda chunk, index: {
        "name": "离线校赛", "tracks": "个人赛" if index == 1 else "团体赛",
    })
    info, error = await llm.extract_contest("x" * 7500)
    assert info is None and "字段冲突：tracks" in error


async def test_eight_request_budget_and_overflow_before_first_request(monkeypatch):
    monkeypatch.setattr(llm.config, "LLM_MAX_INPUT_CHARS", 100)
    calls = model_transport(monkeypatch, lambda chunk, index: {"name": "离线校赛"})
    info, error = await llm.extract_contest("x" * 716)
    assert error is None and info["name"] == "离线校赛" and len(calls) == 8
    info, error = await llm.extract_contest("x" * 717)
    assert info is None and "超过完整提取预算" in error and len(calls) == 8


async def test_total_timeout_cancels_remaining_work_without_partial_success(source, monkeypatch):
    cid, state = source
    state["body"] = ("x" * 7500).encode()
    old_hash = db.list_sources(cid)[0]["body_hash"]
    cancelled = []

    async def stalled(system, user):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    chat = AsyncMock(side_effect=stalled)
    monkeypatch.setattr(llm, "_chat_once", chat)
    monkeypatch.setattr(llm, "_EXTRACTION_TIMEOUT_SECONDS", 0.01)
    results = await crawler.run_fetch(now=NOW)
    assert results[0]["kind"] == "error" and "总等待上限" in results[0]["error"]
    assert cancelled == [True] and chat.await_count == 1
    assert db.get_pending_update(cid) is None and db.list_sources(cid)[0]["body_hash"] == old_hash


@pytest.mark.parametrize("limit", [0, -1])
async def test_invalid_chunk_budget_does_not_call_model(monkeypatch, limit):
    monkeypatch.setattr(llm.config, "LLM_MAX_INPUT_CHARS", limit)
    chat = AsyncMock()
    monkeypatch.setattr(llm, "_chat_once", chat)
    info, error = await llm.extract_contest("比赛公告")
    assert info is None and "上限必须为正数" in error
    chat.assert_not_awaited()
