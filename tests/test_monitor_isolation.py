"""监控单来源失败隔离、最后成功基线和模型输出结构回归。"""

import asyncio
import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import httpx

import crawler
import database
import llm


NOW = datetime(2026, 11, 1, 8, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def monitored(monkeypatch):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    database.init_db()
    monkeypatch.setattr(crawler, "AUTO_FETCH_ENABLED", True)
    healthy = database.create_competition({"name": "healthy"})
    healthy_source = database.add_competition_source(healthy, "https://healthy.example.com")
    bad = database.create_competition({"name": "bad"}, status="confirmed")
    bad_source = database.add_competition_source(bad, "https://bad.example.com")
    database.update_source_fetch(
        bad_source, etag="last-good-etag", last_modified="last-good-modified",
        body_hash=crawler._body_hash("old successful body"), last_status="confirmed",
        last_fetched_at="2026-10-30 08:00:00",
    )
    yield {"conn": conn, "bad": bad, "bad_source": bad_source,
           "healthy": healthy, "healthy_source": healthy_source}
    conn.close()


async def fetch_ok(url):
    return ("bad" if "bad." in url else "healthy"), {"etag": "new-etag"}


async def extract_ok(text):
    return {"name": text}, None


@pytest.mark.parametrize("stage", [
    "fetch", "fetch-shape", "fetch-text", "headers", "extract",
    "extract-shape", "schema", "lookup", "pending", "draft",
    "nochange-state", "success-state", "error-state",
])
async def test_each_source_failure_preserves_baseline_and_continues(monitored, monkeypatch, stage):
    bad_cid = monitored["bad"]
    bad_sid = monitored["bad_source"]
    if stage == "draft":
        database.set_competition_status(bad_cid, "draft")

    async def fetch(url):
        if "bad." in url:
            if stage in ("fetch", "error-state"):
                raise ConnectionError("injected fetch failure")
            if stage == "fetch-shape":
                return None
            if stage == "fetch-text":
                return {"unexpected": "text"}, {}
            if stage == "headers":
                return "bad", {"etag": ["invalid"]}
            if stage == "nochange-state":
                return "old successful body", {"etag": "new-etag"}
        return await fetch_ok(url)

    async def extract(text):
        if text == "bad":
            if stage == "extract":
                raise RuntimeError("injected extract failure")
            if stage == "extract-shape":
                return None
            if stage == "schema":
                return {"name": "bad", "contest_time": []}, None
        return await extract_ok(text)

    if stage == "lookup":
        real_get = database.get_competition

        def get(cid):
            if cid == bad_cid:
                raise sqlite3.OperationalError("injected lookup failure")
            return real_get(cid)

        monkeypatch.setattr(database, "get_competition", get)
    if stage in ("pending", "draft"):
        operation = "add_pending_update" if stage == "pending" else "update_competition"
        if hasattr(database, "store_crawled_competition"):
            operation = "store_crawled_competition"
        real_store = getattr(database, operation)

        def store(cid, *args, **kwargs):
            target_cid = cid.get("competition_id") if isinstance(cid, dict) else cid
            if target_cid == bad_cid:
                raise sqlite3.OperationalError("injected storage failure")
            return real_store(cid, *args, **kwargs)

        monkeypatch.setattr(database, operation, store)
    if stage in ("nochange-state", "success-state", "error-state"):
        real_update = database.update_source_fetch

        def update(sid, **kwargs):
            if sid == bad_sid:
                raise sqlite3.OperationalError("injected source status failure")
            return real_update(sid, **kwargs)

        monkeypatch.setattr(database, "update_source_fetch", update)

    results = await crawler.run_fetch(now=NOW, fetch=fetch, extract=extract)
    assert [r["source_id"] for r in results] == [bad_sid, monitored["healthy_source"]]
    assert results[0]["kind"] == "error"
    assert results[1]["kind"] == "updated_auto"
    source = database.list_sources(bad_cid)[0]
    assert source["etag"] == "last-good-etag"
    assert source["last_modified"] == "last-good-modified"
    assert source["body_hash"] == crawler._body_hash("old successful body")
    healthy = database.list_sources(monitored["healthy"])[0]
    assert healthy["body_hash"] == crawler._body_hash("healthy")


@pytest.mark.parametrize("disabled", ["active", "last_status", "missing-competition", "invalid-status"])
async def test_invalid_or_disabled_sources_are_not_fetched(monitored, monkeypatch, disabled):
    real_list = database.list_sources
    real_get = database.get_competition

    def sources(*args, **kwargs):
        rows = real_list(*args, **kwargs)
        for source in rows:
            if source["id"] == monitored["bad_source"]:
                if disabled == "active":
                    source["active"] = 0
                if disabled == "last_status":
                    source["last_status"] = "disabled"
        return rows

    def get(cid):
        if cid == monitored["bad"]:
            if disabled == "missing-competition":
                return None
            if disabled == "invalid-status":
                return {**real_get(cid), "status": "deleted"}
        return real_get(cid)

    monkeypatch.setattr(database, "list_sources", sources)
    monkeypatch.setattr(database, "get_competition", get)
    fetched = []

    async def fetch(url):
        fetched.append(url)
        return await fetch_ok(url)

    results = await crawler.run_fetch(now=NOW, fetch=fetch, extract=extract_ok)
    assert results[0]["kind"] == "skip"
    assert fetched == ["https://healthy.example.com"]


async def test_source_disabled_during_extraction_is_not_written(monitored, monkeypatch):
    real_list = database.list_sources
    disabled = False

    def sources(*args, **kwargs):
        rows = real_list(*args, **kwargs)
        if disabled:
            rows = [s for s in rows if s["id"] != monitored["bad_source"]]
        return rows

    async def extract(text):
        nonlocal disabled
        if text == "bad":
            disabled = True
            if hasattr(database, "source_snapshot_matches"):
                monitored["conn"].execute(
                    "UPDATE competition_sources SET active = 0 WHERE id = ?", (monitored["bad_source"],),
                )
                monitored["conn"].commit()
        return await extract_ok(text)

    monkeypatch.setattr(database, "list_sources", sources)
    results = await crawler.run_fetch(now=NOW, fetch=fetch_ok, extract=extract)
    assert results[0]["kind"] == "skip"
    assert results[1]["kind"] == "updated_auto"
    assert database.get_pending_update(monitored["bad"]) is None


async def test_competition_deleted_during_unchanged_fetch_is_not_recorded(monitored, monkeypatch):
    real_get = database.get_competition
    deleted = False

    def get(cid):
        return None if deleted and cid == monitored["bad"] else real_get(cid)

    async def fetch(url):
        nonlocal deleted
        if "bad." in url:
            deleted = True
            return "old successful body", {"etag": "new-etag"}
        return await fetch_ok(url)

    monkeypatch.setattr(database, "get_competition", get)
    results = await crawler.run_fetch(now=NOW, fetch=fetch, extract=extract_ok)
    assert results[0]["kind"] == "skip"
    assert results[1]["kind"] == "updated_auto"
    assert database.list_sources(monitored["bad"])[0]["etag"] == "last-good-etag"


@pytest.mark.parametrize("stage,outcome", [
    ("fetch", "changed"), ("fetch", "nochange"), ("fetch", "raises"), ("fetch", "malformed"),
    ("extract", "changed"), ("extract", "raises"),
    ("extract", "reported-error"), ("extract", "malformed"),
])
async def test_same_url_admin_baseline_wins_over_inflight_scan(monitored, stage, outcome):
    entered = asyncio.Event()
    resume = asyncio.Event()

    async def wait_for_admin():
        entered.set()
        await resume.wait()
        if outcome == "raises":
            raise RuntimeError("old scan failed after admin update")

    async def fetch(url):
        if "bad." in url and stage == "fetch":
            await wait_for_admin()
            if outcome == "malformed":
                return None
            if outcome == "nochange":
                return "old successful body", {"etag": "stale-etag"}
        return await fetch_ok(url)

    async def extract(text):
        if text == "bad" and stage == "extract":
            await wait_for_admin()
            if outcome == "reported-error":
                return None, "old extraction failed"
            if outcome == "malformed":
                return None
        return await extract_ok(text)

    scan = asyncio.create_task(crawler.run_fetch(now=NOW, fetch=fetch, extract=extract))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        url = "https://bad.example.com"
        admin_patch = {"name": "ADMIN current", "official_site": url}
        admin_text = "ADMIN latest body"
        if hasattr(database, "source_snapshot_matches"):
            # 来源同步修复合入后，走真正的同 URL 重新提取事务。
            assert database.amend_competition(monitored["bad"], admin_patch, source_text=admin_text)
        else:
            assert database.amend_competition(monitored["bad"], admin_patch)
            database.update_source_fetch(
                monitored["bad_source"], body_hash=crawler._body_hash(admin_text),
                last_status="confirmed",
            )
        admin_source = database.list_sources(monitored["bad"])[0]
        resume.set()
        results = await asyncio.wait_for(scan, timeout=1)
    finally:
        resume.set()
        if not scan.done():
            scan.cancel()
            with pytest.raises(asyncio.CancelledError):
                await scan

    assert results[0]["kind"] == "skip"
    assert results[1]["kind"] == "updated_auto"
    assert database.get_competition(monitored["bad"])["name"] == "ADMIN current"
    assert database.get_pending_update(monitored["bad"]) is None
    assert database.list_sources(monitored["bad"])[0] == admin_source


@pytest.mark.skipif(not hasattr(database, "store_crawled_competition"), reason="依赖来源同步的事务写入接口")
@pytest.mark.parametrize("status", ["draft", "confirmed"])
@pytest.mark.parametrize("change", ["baseline", "url"])
async def test_atomic_store_rechecks_after_last_snapshot_read(monitored, monkeypatch, status, change):
    cid = monitored["bad"]
    if status == "draft":
        database.set_competition_status(cid, "draft")
    real_check = crawler._source_still_active
    checks = 0
    admin_sources = None

    def check(source):
        nonlocal checks, admin_sources
        valid = real_check(source)
        if source["id"] == monitored["bad_source"]:
            checks += 1
            if checks == 2:
                # 精确注入最终读快照之后、写事务开始之前的另一个写入。
                url = "https://new.example.com" if change == "url" else source["url"]
                assert database.amend_competition(
                    cid, {"name": "ADMIN current", "official_site": url}, source_text="ADMIN latest body",
                )
                admin_sources = database.list_sources(cid, active_only=False)
        return valid

    monkeypatch.setattr(crawler, "_source_still_active", check)
    results = await crawler.run_fetch(now=NOW, fetch=fetch_ok, extract=extract_ok)
    assert results[0]["kind"] == "skip"
    assert results[1]["kind"] == "updated_auto"
    assert database.get_competition(cid)["name"] == "ADMIN current"
    assert database.get_pending_update(cid) is None
    assert database.list_sources(cid, active_only=False) == admin_sources


@pytest.mark.skipif(not hasattr(database, "store_crawled_competition"), reason="依赖来源同步的版本和事务接口")
@pytest.mark.parametrize("stage", ["fetch", "extract"])
@pytest.mark.parametrize("fail", [False, True])
async def test_same_metadata_reextract_still_invalidates_scan(monitored, monkeypatch, stage, fail):
    monkeypatch.setattr(database, "now_str", lambda: "2026-10-30 08:00:00")
    cid, sid = monitored["bad"], monitored["bad_source"]
    body = "old successful body"
    database.update_source_fetch(sid, body_hash=crawler._body_hash(body), last_status="confirmed")
    before = database.list_sources(cid)[0]
    entered, resume = asyncio.Event(), asyncio.Event()

    async def pause():
        entered.set()
        await resume.wait()
        if fail:
            raise RuntimeError("old task failed")

    async def fetch(url):
        if "bad." in url and stage == "fetch":
            await pause()
        return await fetch_ok(url)

    async def extract(text):
        if text == "bad" and stage == "extract":
            await pause()
        return await extract_ok(text)

    scan = asyncio.create_task(crawler.run_fetch(now=NOW, fetch=fetch, extract=extract))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert database.amend_competition(
            cid, {"name": "ADMIN current", "official_site": before["url"]}, source_text=body,
        )
        after = database.list_sources(cid)[0]
        assert after["revision"] > before["revision"]
        assert {k: v for k, v in after.items() if k != "revision"} == {
            k: v for k, v in before.items() if k != "revision"
        }
        resume.set()
        results = await asyncio.wait_for(scan, timeout=1)
    finally:
        resume.set()
        if not scan.done():
            scan.cancel()
            with pytest.raises(asyncio.CancelledError):
                await scan
    assert results[0]["kind"] == "skip"
    assert results[1]["kind"] == "updated_auto"
    assert database.get_competition(cid)["name"] == "ADMIN current"
    assert database.get_pending_update(cid) is None
    assert database.list_sources(cid)[0] == after


@pytest.mark.parametrize("content", [None, False, 1, {}, [], [{"type": "text", "text": "hello"}]])
async def test_nontext_model_content_returns_normal_error(monkeypatch, content):
    async def chat(system, user):
        return content

    monkeypatch.setattr(llm, "_chat_once", chat)
    info, err = await llm.extract_contest("比赛信息")
    assert info is None and isinstance(err, str) and err
    info, err = llm.parse_contest_result(content)
    assert info is None and isinstance(err, str) and err


@pytest.mark.parametrize("payload", [
    {}, {"choices": []}, {"choices": [{"message": {}}]},
    {"choices": [{"message": {"content": {"unexpected": "object"}}}]},
    {"choices": [{"message": {"content": [{"text": "hello"}]}}]},
])
async def test_invalid_api_response_is_normal_extraction_error(monkeypatch, payload):
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))
    info, err = await llm.extract_contest("比赛信息")
    assert info is None and isinstance(err, str) and err


def test_excessively_nested_model_json_is_normal_error():
    info, err = llm.parse_contest_result("[" * 2000 + "0" + "]" * 2000)
    assert info is None and err


def test_mapper_omits_unknown_and_empty_fields():
    info, err = llm.validate_contest_info({"name": "new", "raw_note": "", "summary": None})
    assert err is None
    assert crawler._map_competition_info(info) == {"name": "new"}


async def test_draft_update_keeps_fields_not_extracted(monitored):
    cid = monitored["healthy"]
    database.update_competition(cid, {
        "official_reg_deadline": "2026-12-01 09:00", "official_rules": "keep rules",
        "summary": "keep summary", "tracks": "keep tracks", "device_req": "keep device",
    })
    results = await crawler.run_fetch(now=NOW, fetch=fetch_ok, extract=extract_ok)
    assert results[1]["kind"] == "updated_auto"
    comp = database.get_competition(cid)
    assert comp["official_reg_deadline"] == "2026-12-01 09:00"
    assert comp["official_rules"] == "keep rules"
    assert comp["summary"] == "keep summary"
    assert comp["tracks"] == "keep tracks"
    assert comp["device_req"] == "keep device"
    pending = database.get_pending_update(monitored["bad"])
    assert pending["fields"] == {"name": "bad", "official_site": "https://bad.example.com"}


def test_official_optional_fields_survive_validation_and_mapping():
    info, err = llm.parse_contest_result(json.dumps({
        "name": "new", "edition": "2026", "tracks": "A", "device_req": "laptop",
        "official_reg_start": "2026-11-01", "summary": "details",
    }))
    assert err is None
    assert crawler._map_competition_info(info) == {
        "name": "new", "edition": "2026", "tracks": "A", "device_req": "laptop",
        "official_reg_start": "2026-11-01 00:00", "summary": "details",
    }
