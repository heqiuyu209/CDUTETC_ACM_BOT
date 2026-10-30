"""官网来源切换、正文基线和待审部分更新的真实 SQLite / 群命令回归。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from botpy.message import GroupMessage

import commands
import config
import crawler
import database
import main
from fetcher import FetchError


OLD_URL = "https://contest.example.com/old"
NEW_URL = "https://contest.example.com/new?a=1&b=2"
PAGE = "  原比赛公告：2026 年个人赛\n"
NOW = "2026-11-01 08:00:00"
FIELDS = {
    "name": "校赛", "edition": "2026", "official_site": OLD_URL,
    "official_reg_start": "2026-11-01 09:00", "official_reg_deadline": "2026-11-10 23:59",
    "official_time": "2026-12-01 09:00", "tracks": "个人赛", "official_rules": "原规则",
    "device_req": "携带电脑", "summary": "原摘要",
}


@pytest.fixture
def db_file(monkeypatch, tmp_path):
    path = tmp_path / "source-sync.db"
    connections = []

    def connect():
        conn = sqlite3.connect(path, check_same_thread=False)
        connections.append(conn)
        return conn

    monkeypatch.setattr(database, "_connect", connect)
    monkeypatch.setattr(database, "now_str", lambda: NOW)
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(crawler, "AUTO_FETCH_ENABLED", True)
    database.init_db()
    yield path
    for conn in connections:
        conn.close()


@pytest.fixture
def competition(db_file):
    cid = database.create_published_competition(FIELDS, OLD_URL, PAGE, operator="teacher")
    source = database.list_sources(cid)[0]
    database.update_source_fetch(
        source["id"], etag='"original"', last_modified="original-modified",
        body_hash=source["body_hash"], last_status="confirmed",
    )
    return cid


def _snapshot(cid):
    return (
        database.get_competition(cid), database.list_sources(cid, active_only=False),
        database.list_competition_versions(cid), database.get_pending_update(cid), database.list_audit(),
    )


async def test_site_command_switches_effective_source_without_inventing_baseline(competition):
    cid = competition
    old = database.list_sources(cid)[0]
    database.add_pending_update(cid, {"summary": "旧来源的待审"}, source_id=old["id"])
    replies = await commands.handle_command(f"/修正 {cid} site={NEW_URL}", "g1", "admin")

    assert "已修正" in replies[0]["text"]
    assert database.get_competition(cid)["official_site"] == NEW_URL
    active = database.list_sources(cid)
    assert len(active) == 1 and active[0]["url"] == NEW_URL
    assert active[0]["body_hash"] == active[0]["etag"] == active[0]["last_modified"] == ""
    assert active[0]["last_fetched_at"] == ""
    retired = next(source for source in database.list_sources(cid, active_only=False) if source["id"] == old["id"])
    assert retired == {**old, "active": False, "revision": old["revision"] + 1}
    assert database.get_pending_update(cid) is None
    assert database.list_competition_versions(cid)[0]["kind"] == "amend"


@pytest.mark.parametrize("patch", [{"summary": "新摘要"}, {"official_site": OLD_URL}])
def test_same_site_or_other_field_correction_preserves_real_baseline(competition, patch):
    before = database.list_sources(competition)
    assert database.amend_competition(competition, patch)
    assert database.list_sources(competition) == before


def test_clearing_site_disables_sources_and_round_trip_preserves_known_baseline(competition):
    cid = competition
    original = database.list_sources(cid)[0]
    assert database.amend_competition(cid, {"official_site": ""})
    assert database.list_sources(cid) == []
    assert database.list_sources(cid, active_only=False) == [{**original, "active": False, "revision": original["revision"] + 1}]
    assert database.amend_competition(cid, {"official_site": NEW_URL})
    assert database.amend_competition(cid, {"official_site": OLD_URL})
    restored = database.list_sources(cid)[0]
    assert restored == {**original, "revision": original["revision"] + 2}
    assert len(database.list_sources(cid, active_only=False)) == 2


def test_same_url_in_another_competition_never_supplies_new_site_baseline(competition):
    other = database.create_published_competition({"name": "另一个比赛"}, NEW_URL, "另一个正文")
    other_source = database.list_sources(other)[0]
    assert database.amend_competition(competition, {"official_site": NEW_URL})
    assert database.list_sources(competition)[0]["body_hash"] == ""
    assert database.list_sources(other) == [other_source]
    assert database.get_source_by_url(NEW_URL, competition)["competition_id"] == competition


@pytest.mark.parametrize("failure", ["source", "version", "audit"])
def test_source_switch_failure_rolls_back_fields_history_pending_and_audit(competition, monkeypatch, failure):
    cid = competition
    database.add_pending_update(cid, {"summary": "待审摘要"})
    before = _snapshot(cid)
    if failure == "source":
        with database._connect() as conn:
            conn.execute("CREATE TRIGGER reject_source BEFORE INSERT ON competition_sources BEGIN SELECT RAISE(ABORT, 'source insert failed'); END")
    else:
        def fail(*args, **kwargs):
            raise sqlite3.OperationalError("write failed")
        monkeypatch.setattr(database, "_add_version" if failure == "version" else "add_audit", fail)
    with pytest.raises(sqlite3.Error):
        database.amend_competition(cid, {"name": "新名称", "official_site": NEW_URL}, source_text="新正文")
    assert _snapshot(cid) == before


@pytest.mark.parametrize("site", ["not-a-url", "ftp://contest.example.com/", "https://user:pass@contest.example.com/"])
def test_invalid_site_patch_has_no_database_effect(competition, site):
    before = _snapshot(competition)
    with pytest.raises(ValueError):
        database.amend_competition(competition, {"summary": "新摘要", "official_site": site})
    assert _snapshot(competition) == before


async def test_refetch_uses_actual_url_and_body_and_preserves_unextracted_fields(competition, monkeypatch):
    cid = competition
    page = "  新官网正文\n新的截止时间\n"
    fetch = AsyncMock(return_value=page)
    extract = AsyncMock(return_value=({
        "name": "更新校赛", "reg_deadline": "2026-11-20 23:59", "raw_note": "",
        "source_link": "https://invented.example.com/never-fetched",
    }, None))
    monkeypatch.setattr(commands, "fetch_text", fetch)
    monkeypatch.setattr(commands.llm, "extract_contest", extract)
    replies = await commands.handle_command(f"/修正 {cid} 重新提取 {NEW_URL}", "g1", "admin")

    fetch.assert_awaited_once_with(NEW_URL)
    assert "已重新提取" in replies[0]["text"]
    comp = database.get_competition(cid)
    assert comp["official_site"] == NEW_URL and comp["name"] == "更新校赛"
    assert comp["official_reg_deadline"] == "2026-11-20 23:59"
    for field in ("edition", "tracks", "device_req", "official_reg_start", "official_time", "official_rules", "summary"):
        assert comp[field] == FIELDS[field]
    source = database.list_sources(cid)[0]
    assert source["url"] == NEW_URL and source["body_hash"] == hashlib.sha256(page.encode()).hexdigest()
    assert source["last_status"] == "confirmed"

    fetched = []
    async def fetch_again(url):
        fetched.append(url)
        return page, {}
    async def no_extract(text):
        pytest.fail("刚重新提取的相同正文不应再次调用模型")
    results = await crawler.run_fetch(
        now=datetime(2026, 11, 1, 10, tzinfo=ZoneInfo("Asia/Shanghai")), fetch=fetch_again, extract=no_extract,
    )
    assert fetched == [NEW_URL] and results[0]["kind"] == "nochange"


async def test_refetch_retains_new_official_optional_fields(competition, monkeypatch):
    monkeypatch.setattr(commands, "fetch_text", AsyncMock(return_value="新正文"))
    new_fields = {"name": "校赛", "edition": "2027", "tracks": "团体赛", "device_req": "自备键盘", "official_reg_start": "2026-11-02 09:00"}
    monkeypatch.setattr(commands.llm, "extract_contest", AsyncMock(return_value=(new_fields, None)))
    replies = await commands.handle_command(f"/修正 {competition} 重新提取 {OLD_URL}", "g1", "admin")
    assert "已重新提取" in replies[0]["text"]
    comp = database.get_competition(competition)
    assert {field: comp[field] for field in new_fields} == new_fields


@pytest.mark.parametrize("failure", ["fetch", "empty", "extract_exception", "extract_error", "missing_name", "bad_time", "bad_type", "not_object"])
async def test_refetch_failures_preserve_all_existing_state(competition, monkeypatch, failure):
    database.add_pending_update(competition, {"summary": "现有待审"})
    before = _snapshot(competition)
    fetch = AsyncMock(return_value="新正文")
    extract = AsyncMock(return_value=({"name": "新名称"}, None))
    if failure == "fetch":
        fetch.side_effect = FetchError("模拟抓取失败")
    elif failure == "empty":
        fetch.return_value = "  "
    elif failure == "extract_exception":
        extract.side_effect = RuntimeError("模型异常")
    elif failure == "extract_error":
        extract.return_value = (None, "模型失败")
    elif failure == "missing_name":
        extract.return_value = ({"reg_deadline": "2026-11-20 23:59"}, None)
    elif failure == "bad_time":
        extract.return_value = ({"name": "新名称", "reg_deadline": "2026-02-30 09:00"}, None)
    elif failure == "bad_type":
        extract.return_value = ({"name": "新名称", "device_req": ["非法类型"]}, None)
    else:
        extract.return_value = (["非法对象"], None)
    monkeypatch.setattr(commands, "fetch_text", fetch)
    monkeypatch.setattr(commands.llm, "extract_contest", extract)
    replies = await commands.handle_command(f"/修正 {competition} 重新提取 {NEW_URL}", "g1", "admin")
    assert len(replies) == 1 and "已重新提取" not in replies[0]["text"]
    assert _snapshot(competition) == before


def test_pending_sparse_patch_preserves_current_fields_and_source(competition):
    cid = competition
    original = database.get_competition(cid)
    sources = database.list_sources(cid)
    uid = database.add_pending_update(cid, {"official_reg_deadline": "2026-11-20 23:59"}, source_id=sources[0]["id"])
    assert database.apply_pending_update(uid, operator="admin")
    updated = database.get_competition(cid)
    for field in FIELDS:
        assert updated[field] == ("2026-11-20 23:59" if field == "official_reg_deadline" else original[field])
    assert updated["status"] == "confirmed" and database.list_sources(cid) == sources
    assert database.get_pending_update(cid) is None
    saved = json.loads(database.list_competition_versions(cid)[0]["fields_json"])
    assert saved["edition"] == FIELDS["edition"] and saved["device_req"] == FIELDS["device_req"]
    assert database.list_audit()[0]["operator"] == "admin"


def test_legacy_pending_empty_defaults_do_not_clear_confirmed_fields(competition):
    uid = database.add_pending_update(competition, {"name": "新校赛"})
    fields = {**{field: "" for field in FIELDS}, "name": "新校赛", "official_reg_start": None}
    with database._connect() as conn:
        conn.execute("UPDATE competition_pending_updates SET fields_json = ? WHERE id = ?", (json.dumps(fields), uid))
    assert database.apply_pending_update(uid)
    comp = database.get_competition(competition)
    assert comp["name"] == "新校赛"
    for field in FIELDS:
        if field != "name":
            assert comp[field] == FIELDS[field]


@pytest.mark.parametrize("raw", ["broken-json", "[]", "null", "{}", '{"name":""}', '{"name":123}', '{"unknown":"value"}', '{"official_time":"2026-02-30 09:00"}'])
def test_corrupt_pending_approval_preserves_existing_data(competition, raw):
    uid = database.add_pending_update(competition, {"summary": "新摘要"})
    with database._connect() as conn:
        conn.execute("UPDATE competition_pending_updates SET fields_json = ? WHERE id = ?", (raw, uid))
    before = _snapshot(competition)
    with pytest.raises(ValueError):
        database.apply_pending_update(uid)
    assert _snapshot(competition) == before


def test_pending_site_switch_without_fetched_text_has_empty_baseline(competition):
    uid = database.add_pending_update(competition, {"official_site": NEW_URL})
    assert database.apply_pending_update(uid)
    assert database.get_competition(competition)["official_site"] == NEW_URL
    active = database.list_sources(competition)
    assert len(active) == 1 and active[0]["url"] == NEW_URL and active[0]["body_hash"] == ""


def test_inactive_source_cannot_write_fetch_metadata_or_new_pending(competition):
    old = database.list_sources(competition)[0]
    assert database.amend_competition(competition, {"official_site": NEW_URL})
    history = database.list_sources(competition, active_only=False)
    database.update_source_fetch(old["id"], body_hash="stale-hash", last_status="pending")
    assert database.list_sources(competition, active_only=False) == history
    with pytest.raises(ValueError, match="停用"):
        database.add_pending_update(competition, {"summary": "旧来源在途结果"}, source_id=old["id"])
    assert database.get_pending_update(competition) is None


def test_pending_source_must_belong_to_competition_and_match_actual_url(competition):
    other = database.create_published_competition({"name": "其他比赛"}, OLD_URL, "其他正文")
    other_sid = database.list_sources(other)[0]["id"]
    with pytest.raises(ValueError, match="不属于"):
        database.add_pending_update(competition, {"summary": "错误来源"}, source_id=other_sid)
    sid = database.list_sources(competition)[0]["id"]
    with pytest.raises(ValueError, match="不一致"):
        database.add_pending_update(competition, {"official_site": NEW_URL}, source_id=sid)
    uid = database.add_pending_update(competition, {"summary": "有效来源"}, source_id=sid)
    with database._connect() as conn:
        conn.execute("UPDATE competition_pending_updates SET fields_json = ? WHERE id = ?", (json.dumps({"official_site": NEW_URL}), uid))
    before = _snapshot(competition)
    with pytest.raises(ValueError, match="不一致"):
        database.apply_pending_update(uid)
    assert _snapshot(competition) == before


def test_concurrent_approval_applies_once_across_connections(competition):
    uid = database.add_pending_update(competition, {"summary": "新摘要"})
    version_count = len(database.list_competition_versions(competition))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(database.apply_pending_update, [uid, uid]))
    assert sorted(results) == [False, True]
    assert len(database.list_competition_versions(competition)) == version_count + 1
    assert sum(row["action"] == "apply_pending_update" for row in database.list_audit()) == 1


def _fetch_state(text="新正文"):
    return {
        "etag": '"new"', "last_modified": "new-modified", "body_hash": hashlib.sha256(text.encode()).hexdigest(),
        "last_fetched_at": "2026-11-01 10:00:00",
    }


@pytest.mark.parametrize("status", ["draft", "confirmed"])
@pytest.mark.parametrize("change", ["url", "body", "identical_body_same_second"])
def test_atomic_crawler_store_rejects_stale_source_even_after_final_read(competition, status, change):
    cid = competition
    if status == "draft":
        database.set_competition_status(cid, "draft")
    if change == "identical_body_same_second":
        current = database.list_sources(cid)[0]
        database.update_source_fetch(current["id"], body_hash=current["body_hash"], last_status="confirmed")
    scanned = database.list_sources(cid)[0]
    assert database.source_snapshot_matches(scanned)  # 模拟 crawler 的最终独立检查已通过。
    if change == "url":
        database.amend_competition(cid, {"official_site": NEW_URL})
    else:
        database.amend_competition(
            cid, {"summary": "人工最新摘要", "official_site": OLD_URL},
            source_text=PAGE if change == "identical_body_same_second" else "人工最新正文",
        )
    before = _snapshot(cid)
    if change == "identical_body_same_second":
        current = database.list_sources(cid)[0]
        assert current == {**scanned, "revision": scanned["revision"] + 1}
    assert not database.source_snapshot_matches(scanned)
    assert database.store_crawled_competition(scanned, {"summary": "STALE", "official_site": OLD_URL}, _fetch_state()) is None
    assert not database.update_source_fetch(scanned["id"], expected_source=scanned, last_status="error", last_error="STALE")
    assert _snapshot(cid) == before


@pytest.mark.parametrize("status", ["draft", "confirmed"])
def test_atomic_crawler_store_saves_content_and_baseline_together(competition, status):
    cid = competition
    if status == "draft":
        database.set_competition_status(cid, "draft")
    source = database.list_sources(cid)[0]
    result = database.store_crawled_competition(source, {"summary": "新摘要"}, _fetch_state())
    assert result["competition_id"] == cid
    stored = database.list_sources(cid)[0]
    assert stored["body_hash"] == _fetch_state()["body_hash"] and stored["revision"] == source["revision"] + 1
    if status == "confirmed":
        assert result["action"] == "pending" and result["update_id"] > 0
        assert database.get_competition(cid)["summary"] == FIELDS["summary"]
        assert database.get_pending_update(cid)["fields"] == {"summary": "新摘要"}
        assert stored["last_status"] == "pending"
    else:
        assert result["action"] == "updated" and database.get_competition(cid)["summary"] == "新摘要"
        assert database.get_competition(cid)["edition"] == FIELDS["edition"]
        assert stored["last_status"] == "draft"


def test_atomic_crawler_pending_failure_rolls_back_pending_and_baseline(competition):
    cid = competition
    database.add_pending_update(cid, {"summary": "已有待审"})
    before = _snapshot(cid)
    with database._connect() as conn:
        conn.execute("CREATE TRIGGER reject_baseline BEFORE UPDATE ON competition_sources BEGIN SELECT RAISE(ABORT, 'baseline write failed'); END")
    with pytest.raises(sqlite3.Error):
        database.store_crawled_competition(database.list_sources(cid)[0], {"summary": "新待审"}, _fetch_state())
    assert _snapshot(cid) == before


def test_atomic_crawler_draft_failure_rolls_back_fields_version_and_baseline(competition, monkeypatch):
    cid = competition
    database.set_competition_status(cid, "draft")
    before = _snapshot(cid)
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("version write failed")
    monkeypatch.setattr(database, "_add_version", fail)
    with pytest.raises(sqlite3.Error):
        database.store_crawled_competition(database.list_sources(cid)[0], {"summary": "新待审"}, _fetch_state())
    assert _snapshot(cid) == before


@pytest.mark.parametrize("command", ["待审列表", "待审批准", "待审拒绝"])
@pytest.mark.parametrize("sender", ["teacher", "member"])
async def test_pending_permissions_reject_before_any_database_access(db_file, monkeypatch, command, sender):
    def forbidden(*args, **kwargs):
        pytest.fail("待审命令未授权时不应访问数据库")
    for name in ("list_pending_updates", "apply_pending_update", "reject_pending_update"):
        monkeypatch.setattr(database, name, forbidden)
    replies = await commands.handle_command(f"/{command} 1", "g1", sender)
    assert len(replies) == 1 and "仅超管" in replies[0]["text"] and replies[0]["group_id"] == "g1"


async def test_real_sdk_pending_commands_only_send_source_group_confirmations(competition, monkeypatch):
    client = main.ACMBot()
    api = SimpleNamespace(post_group_message=AsyncMock(return_value={"id": "sent"}), post_c2c_message=AsyncMock())
    monkeypatch.setattr(client, "api", api)
    uid = database.add_pending_update(competition, {"summary": "私有填充值-private-value"}, change_note="私有原文-private-note", created_by="private-operator")
    async def route(text, msg_id):
        await client.on_group_at_message_create(GroupMessage(api=api, event_id="event", data={
            "id": msg_id, "content": text, "group_openid": "g1", "author": {"member_openid": "admin"},
        }))
    await route("/待审列表", "list-id")
    await route(f"/待审批准 {uid}", "apply-id")
    assert database.get_competition(competition)["summary"] == "私有填充值-private-value"
    uid = database.add_pending_update(competition, {"summary": "不得应用-private-rejected"})
    before = database.get_competition(competition)
    await route(f"/待审拒绝 {uid} 含姓名学号-private-reason", "reject-id")
    assert database.get_competition(competition) == before
    assert database.get_pending_update(competition) is None
    assert api.post_group_message.await_count == 3
    calls = [call.kwargs for call in api.post_group_message.await_args_list]
    assert all(call["group_openid"] == "g1" and "msg_id" in call and call["msg_seq"] == 1 for call in calls)
    assert [call["msg_id"] for call in calls] == ["list-id", "apply-id", "reject-id"]
    assert all("private-" not in call["content"] for call in calls)
    assert "summary" in calls[0]["content"] and "已批准" in calls[1]["content"] and "已拒绝" in calls[2]["content"]
    api.post_c2c_message.assert_not_awaited()
    assert "private-reason" in database.list_audit()[0]["after_json"]


def test_old_source_schema_migrates_effective_site_without_losing_history(tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / "legacy.db")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    monkeypatch.setattr(database, "now_str", lambda: NOW)
    try:
        with conn:
            conn.execute(database._COMPETITIONS_DDL)
            conn.execute("""CREATE TABLE competition_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT, competition_id INTEGER NOT NULL, url TEXT NOT NULL,
                etag TEXT NOT NULL DEFAULT '', last_modified TEXT NOT NULL DEFAULT '', body_hash TEXT NOT NULL DEFAULT '',
                last_fetched_at TEXT NOT NULL DEFAULT '', last_status TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '', UNIQUE(competition_id, url))""")
            conn.execute("INSERT INTO competitions (id, name, official_site, status) VALUES (1, '旧库已修正比赛', ?, 'confirmed')", (NEW_URL,))
            conn.execute("INSERT INTO competition_sources (competition_id, url, body_hash, etag) VALUES (1, ?, ?, ?)", (OLD_URL, hashlib.sha256(PAGE.encode()).hexdigest(), '"old"'))
            conn.execute("INSERT INTO competitions (id, name, status) VALUES (2, '旧库空官网但已登记来源', 'confirmed')")
            conn.execute("INSERT INTO competition_sources (competition_id, url, body_hash, etag) VALUES (2, ?, ?, ?)", (OLD_URL, hashlib.sha256(b"legacy body").hexdigest(), '"legacy"'))
        database.init_db()
        active = database.list_sources(1)
        assert len(active) == 1 and active[0]["url"] == NEW_URL and active[0]["body_hash"] == ""
        history = database.list_sources(1, active_only=False)
        old = next(source for source in history if source["url"] == OLD_URL)
        assert old["active"] is False and old["body_hash"] == hashlib.sha256(PAGE.encode()).hexdigest() and old["etag"] == '"old"'
        legacy = database.list_sources(2)
        assert len(legacy) == 1 and legacy[0]["url"] == OLD_URL and legacy[0]["active"] is True
        assert legacy[0]["body_hash"] == hashlib.sha256(b"legacy body").hexdigest() and legacy[0]["etag"] == '"legacy"'
        database.init_db()
        assert database.list_sources(1, active_only=False) == history
        assert database.list_sources(2) == legacy
    finally:
        conn.close()


def test_failed_old_source_migration_can_retry_without_partial_schema(tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / "legacy-retry.db")
    monkeypatch.setattr(database, "_connect", lambda: conn)
    try:
        legacy_ddl = database._COMPETITION_SOURCES_DDL.replace("    active          INTEGER NOT NULL DEFAULT 1,\n", "").replace("    revision        INTEGER NOT NULL DEFAULT 0,\n", "")
        with conn:
            conn.execute(database._COMPETITIONS_DDL)
            conn.execute(legacy_ddl)
            conn.execute("INSERT INTO competitions (id, name, official_site) VALUES (1, '待迁移比赛', ?)", (NEW_URL,))
            conn.execute("INSERT INTO competition_sources (competition_id, url, body_hash) VALUES (1, ?, ?)", (OLD_URL, hashlib.sha256(PAGE.encode()).hexdigest()))
            conn.execute("CREATE TRIGGER reject_new_source BEFORE INSERT ON competition_sources BEGIN SELECT RAISE(ABORT, 'migration write failed'); END")
        with pytest.raises(sqlite3.Error):
            database.init_db()
        columns = {row[1] for row in conn.execute("PRAGMA table_info(competition_sources)")}
        assert "active" not in columns and "revision" not in columns
        assert conn.execute("SELECT url, body_hash FROM competition_sources").fetchall() == [(OLD_URL, hashlib.sha256(PAGE.encode()).hexdigest())]
        with conn:
            conn.execute("DROP TRIGGER reject_new_source")
        database.init_db()
        assert database.list_sources(1)[0]["url"] == NEW_URL
        assert len(database.list_sources(1, active_only=False)) == 2
    finally:
        conn.close()
