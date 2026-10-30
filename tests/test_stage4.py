"""阶段四：公共来源集中低频抓取、ETag/Last-Modified/正文哈希变化检测、
实质变化才调一次模型、免审发布（首次抓取直接 confirmed）、变化存待审、
v2 不向任何群自动通知、来源异常提示（内存 SQLite 离线测试）。"""

import asyncio
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import crawler
import database

TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    database.init_db()
    # v2：自动抓取默认关闭，测试显式开启
    monkeypatch.setattr(crawler, "AUTO_FETCH_ENABLED", True)
    yield conn
    conn.close()


def _now(hour=8, minute=0):
    return datetime(2026, 11, 1, hour, minute, tzinfo=TZ)


def _new_competition_source(db, name="ICPC 2026", url="https://icpc.example.com/news", confirmed=False, extra=None):
    info = {"name": name, "official_site": url}
    if extra:
        info.update(extra)
    cid = database.create_competition(
        info, status="confirmed" if confirmed else "draft")
    sid = database.add_competition_source(cid, url)
    return cid, sid


async def _fetch_text(url):
    return "页面A：2026 ICPC 区域赛 11 月 10 日截止报名", {"etag": '"aaa"', "last-modified": "2026-10-01 00:00:00"}


async def _extract_ok(text):
    return {"name": "ICPC 2026 区域赛", "reg_deadline": "2026-11-10 23:59",
            "contest_time": "2026-12-05 09:00", "source_link": "https://icpc.example.com/news",
            "raw_note": "官方规则"}, None


# ---------- 变化检测与间隔控制（纯函数） ----------


def test_detect_change_first_fetch_is_change(db):
    source = {"body_hash": ""}
    assert crawler.detect_change(source, "正文", "", "") is True


def test_detect_change_hash_same_no_change(db):
    import hashlib
    body = "稳定页面内容"
    source = {"body_hash": hashlib.sha256(body.encode("utf-8")).hexdigest()}
    # ETag/Last-Modified 变了但正文没变 → 不算实质变化
    assert crawler.detect_change(source, body, '"new-etag"', "2026-10-20 00:00:00") is False


def test_detect_change_hash_differs(db):
    import hashlib
    old = hashlib.sha256("旧内容".encode("utf-8")).hexdigest()
    assert crawler.detect_change({"body_hash": old}, "新内容", "", "") is True


def test_is_within_min_interval(db):
    source = {"last_fetched_at": ""}
    assert crawler.is_within_min_interval(source, _now()) is False
    recent = {"last_fetched_at": _now(hour=7, minute=59).strftime("%Y-%m-%d %H:%M:%S")}
    assert crawler.is_within_min_interval(recent, _now(8)) is True  # 1 分钟前，间隔内
    old = {"last_fetched_at": _now(hour=5).strftime("%Y-%m-%d %H:%M:%S")}
    assert crawler.is_within_min_interval(old, _now(8)) is False  # 3 小时前，可抓


# ---------- 抓取调度与变化检测（集成，注入 mock 回调） ----------


def test_run_fetch_missing_competition_is_skipped(db, monkeypatch):
    """关联比赛缺失的来源跳过，不创建无法正确关联的新比赛。"""
    cid, sid = _new_competition_source(db)
    monkeypatch.setattr(database, "get_competition", lambda _cid: None)
    results = asyncio.run(crawler.run_fetch(
        now=_now(), fetch=_fetch_text, extract=_extract_ok))
    assert len(results) == 1
    r = results[0]
    assert r["kind"] == "skip" and r["source_id"] == sid
    assert len(database.list_competitions()) == 1
    source = database.list_sources(cid)[0]
    assert not source["last_status"] and not source["body_hash"]


def test_run_fetch_updates_existing_draft(db):
    """兼容旧草稿：来源对应比赛仍为 draft 时，抓取结果直接更新草稿（不生成新比赛）。"""
    cid, sid = _new_competition_source(db)
    results = asyncio.run(crawler.run_fetch(
        now=_now(), fetch=_fetch_text, extract=_extract_ok))
    r = results[0]
    assert r["kind"] == "updated_auto" and r["action"] == "updated"
    assert r["competition_id"] == cid and r["notify_groups"] == []
    comp = database.get_competition(cid)
    assert comp["status"] == "draft"
    assert comp["official_reg_deadline"] == "2026-11-10 23:59"
    source = database.list_sources(cid)[0]
    assert source["last_status"] == "draft" and source["body_hash"]


def test_run_fetch_nochange_does_not_call_model(db):
    cid, sid = _new_competition_source(db)
    asyncio.run(crawler.run_fetch(now=_now(), fetch=_fetch_text, extract=_extract_ok))
    extract_calls = []

    async def extract_spy(text):
        extract_calls.append(text)
        return await _extract_ok(text)

    results = asyncio.run(crawler.run_fetch(
        now=_now(hour=10), fetch=_fetch_text, extract=extract_spy))
    assert results[0]["kind"] == "nochange"
    assert extract_calls == []  # 正文哈希未变，不调模型


def test_run_fetch_skip_within_interval(db):
    cid, sid = _new_competition_source(db)
    fetch_calls = []

    async def fetch_spy(url):
        fetch_calls.append(url)
        return await _fetch_text(url)

    asyncio.run(crawler.run_fetch(now=_now(hour=8), fetch=fetch_spy, extract=_extract_ok))
    # 5 分钟后仍在最小间隔内 → 跳过，不再抓取
    results = asyncio.run(crawler.run_fetch(now=_now(hour=8, minute=5), fetch=fetch_spy, extract=_extract_ok))
    assert results[0]["kind"] == "skip" and results[0]["reason"] == "最小抓取间隔未到"
    assert len(fetch_calls) == 1


def test_run_fetch_fetch_error_no_notify(db):
    cid, sid = _new_competition_source(db)

    async def fetch_fail(url):
        raise ConnectionError("网络不可达")

    results = asyncio.run(crawler.run_fetch(now=_now(), fetch=fetch_fail, extract=_extract_ok))
    r = results[0]
    assert r["kind"] == "error" and "网络不可达" in r["error"]
    assert r["notify_groups"] == []  # v2：抓取异常不向任何群通知
    source = database.list_sources(cid)[0]
    assert source["last_status"] == "error" and source["last_error"]


def test_run_fetch_extract_failure_no_write(db):
    cid, sid = _new_competition_source(db)

    async def extract_fail(text):
        return None, "提取结果未通过 schema 校验"

    results = asyncio.run(crawler.run_fetch(now=_now(), fetch=_fetch_text, extract=extract_fail))
    r = results[0]
    assert r["kind"] == "error" and "校验" in r["error"]
    # 未写库：比赛仍为原始草稿，无官方信息；来源状态 error 且未更新正文哈希
    comp = database.get_competition(cid)
    assert comp["official_reg_deadline"] == ""
    source = database.list_sources(cid)[0]
    assert source["last_status"] == "error" and not source["body_hash"]


def test_run_fetch_schema_invalid_mapped_no_write(db):
    cid, sid = _new_competition_source(db)

    async def extract_bad_schema(text):
        # 返回缺 name 等必填字段 → 映射后 create_competition 抛 ValueError
        return {"reg_deadline": "2026-11-10 23:59"}, None

    results = asyncio.run(crawler.run_fetch(now=_now(), fetch=_fetch_text, extract=extract_bad_schema))
    assert results[0]["kind"] == "error"
    source = database.list_sources(cid)[0]
    assert source["last_status"] == "error"
    assert source["body_hash"] == ""  # 校验失败不更新正文哈希，下次仍视为变化


def test_run_fetch_confirmed_changes_go_to_pending_review(db):
    """v2：已确认比赛出现实质变化时，正式版本保持不变，新内容存待审，超管确认后替换。"""
    cid, sid = _new_competition_source(
        db, confirmed=True, extra={"official_reg_deadline": "2026-11-10 23:59"})

    async def fetch_new(url):
        return "页面B：截止时间改为 11 月 20 日！", {"etag": '"bbb"', "last-modified": "2026-10-02 00:00:00"}

    async def extract_new(text):
        return {"name": "ICPC 2026 区域赛", "reg_deadline": "2026-11-20 23:59",
                "contest_time": "2026-12-06 09:00", "source_link": "https://icpc.example.com/news",
                "raw_note": "官方规则更新"}, None

    results = asyncio.run(crawler.run_fetch(now=_now(), fetch=fetch_new, extract=extract_new))
    r = results[0]
    assert r["kind"] == "pending_auto" and r["action"] == "pending"
    assert r["update_id"] > 0
    assert r["notify_groups"] == []  # v2：待审变化不自动通知，等待超管决策
    # 正式内容不被覆盖：状态仍 confirmed，截止时间仍是已确认值
    comp = database.get_competition(cid)
    assert comp["status"] == "confirmed"
    assert comp["official_reg_deadline"] == "2026-11-10 23:59"
    # 新内容存为待审版本
    pending = database.get_pending_update(cid)
    assert pending is not None
    assert pending["fields"].get("official_reg_deadline") == "2026-11-20 23:59"
    # 超管确认后：正式内容替换为待审内容，状态保持 confirmed，待审记录清除
    assert database.apply_pending_update(r["update_id"], operator="t") is True
    comp = database.get_competition(cid)
    assert comp["status"] == "confirmed"
    assert comp["official_reg_deadline"] == "2026-11-20 23:59"
    assert database.get_pending_update(cid) is None


def test_run_fetch_orphan_source_has_no_group_notification(db, monkeypatch):
    cid, sid = _new_competition_source(db)
    monkeypatch.setattr(database, "get_competition", lambda _cid: None)
    sent = []

    async def send(gid, text):
        sent.append((gid, text))
        return True

    results = asyncio.run(crawler.run_fetch(now=_now(), fetch=_fetch_text,
                                            extract=_extract_ok, send_group=send))
    assert results[0]["kind"] == "skip"
    assert len(sent) == 0


def test_run_fetch_second_round_all_skip_within_interval(db):
    cid, sid = _new_competition_source(db)
    asyncio.run(crawler.run_fetch(now=_now(), fetch=_fetch_text, extract=_extract_ok))
    results = asyncio.run(crawler.run_fetch(now=_now(hour=8, minute=10), fetch=_fetch_text, extract=_extract_ok))
    assert all(r["kind"] == "skip" for r in results)
