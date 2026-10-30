"""真实 SQLite 发布幂等回归；模型、网络和 QQ SDK 均不联网。"""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import crawler
import config
import database
import llm
import main
from contest_fields import map_competition_info
from llm import validate_contest_info
from onebot_client import OneBotMessage


URL = "https://contest.example.com/announcement?track=acm"
PAGE = "校赛完整公告正文"
INFO = {
    "name": "校赛",
    "edition": "2026",
    "official_reg_start": "2026-11-01 08:00",
    "official_reg_deadline": "2026-11-10 23:59",
    "official_time": "2026-11-20 09:00",
    "tracks": "个人赛、团队赛",
    "official_rules": "比赛规则原文",
    "device_req": "自带电脑",
    "summary": "完整摘要",
}


@pytest.fixture
def db_file(monkeypatch, tmp_path):
    path = tmp_path / "publication.db"
    connections = []

    def connect():
        conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
        connections.append(conn)
        return conn

    monkeypatch.setattr(database, "_connect", connect)
    database.init_db()
    yield path, connections
    for conn in connections:
        conn.close()


def publish(info=None, url=URL, text=PAGE):
    return database.create_published_competition_once(
        INFO if info is None else info, url, text, operator="publish:teacher"
    )


def counts(path):
    with sqlite3.connect(path) as conn:
        return tuple(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in ("competitions", "competition_versions",
                                   "competition_sources", "publication_claims"))


def test_full_fields_persist_and_int_api_remains_compatible(db_file):
    cid, created = publish()
    assert created is True
    record = database.get_competition(cid)
    for field, value in INFO.items():
        assert record[field] == value
    assert record["official_site"] == URL
    assert database.create_published_competition(INFO, URL, PAGE) == cid
    assert counts(db_file[0]) == (1, 1, 1, 1)


def test_repeated_publication_preserves_amendment_and_monitor_baseline(db_file):
    cid, _ = publish()
    database.update_competition(cid, {
        "name": "人工修正后的名称", "summary": "人工修正摘要", "tracks": "仅个人赛",
        "official_time": "2026-11-25 09:00",
    })
    sid = database.list_sources(cid)[0]["id"]
    database.update_source_fetch(sid, etag='"good"', last_modified="today",
                                 body_hash=hashlib.sha256(b"monitored").hexdigest(),
                                 last_status="nochange", last_fetched_at="2026-11-02 08:00:00")
    before = database.get_competition(cid)
    sources_before = database.list_sources(cid)
    versions_before = database.list_competition_versions(cid)
    result = publish({**INFO, "summary": "", "tracks": ""}, text="新的网页正文")
    assert result == (cid, False)
    assert database.get_competition(cid) == before
    assert database.list_sources(cid) == sources_before
    assert database.list_competition_versions(cid) == versions_before


def test_same_publication_is_atomic_across_independent_connections(db_file):
    gate = Barrier(2)

    def worker():
        gate.wait(timeout=10)
        return publish()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker) for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]
    assert results[0][0] == results[1][0]
    assert sorted(created for _, created in results) == [False, True]
    assert counts(db_file[0]) == (1, 1, 1, 1)


def test_publication_claim_survives_restart_and_name_amendment(db_file):
    cid, _ = publish()
    database.update_competition(cid, {"name": "手工改名"})
    before = database.get_competition(cid)
    for conn in db_file[1]:
        conn.close()
    db_file[1].clear()
    database.init_db()
    assert publish() == (cid, False)
    assert database.get_competition(cid) == before
    assert counts(db_file[0]) == (1, 2, 1, 1)


@pytest.mark.parametrize("amended", [False, True])
def test_legacy_confirmed_publication_gains_claim_without_changing_content(db_file, amended):
    cid = database.create_competition({**INFO, "official_site": URL}, status="confirmed")
    sid = database.add_competition_source(cid, URL)
    database.update_source_fetch(sid, body_hash="successful-old-body", etag='"old"',
                                 last_status="nochange")
    if amended:
        database.update_competition(cid, {"name": "旧库已手工改名", "edition": "修正版"})
        database.set_competition_status(cid, "draft")
    before = database.get_competition(cid)
    sources_before = database.list_sources(cid)
    versions_before = database.list_competition_versions(cid)
    assert publish(text="重新抓到的正文") == (cid, False)
    assert database.get_competition(cid) == before
    assert database.list_sources(cid) == sources_before
    assert database.list_competition_versions(cid) == versions_before
    assert counts(db_file[0])[3] == 1


def test_legacy_confirmed_record_without_source_is_not_published_again(db_file):
    cid = database.create_competition({**INFO, "official_site": URL}, status="confirmed")
    assert publish() == (cid, False)
    assert counts(db_file[0]) == (1, 1, 0, 1)


@pytest.mark.parametrize("raw", ["not-json", "[" * 5000 + "0" + "]" * 5000],
                         ids=["invalid-json", "excessive-nesting"])
def test_bad_historical_version_does_not_block_another_publication(db_file, raw):
    old_cid = database.create_competition({"name": "旧比赛", "official_site": URL}, status="confirmed")
    with sqlite3.connect(db_file[0]) as conn:
        conn.execute("UPDATE competition_versions SET fields_json = ? WHERE competition_id = ?",
                     (raw, old_cid))
    cid, created = publish()
    assert created is True and cid != old_cid
    assert publish() == (cid, False)
    assert counts(db_file[0]) == (2, 2, 1, 1)


@pytest.mark.parametrize("original,equivalent", [
    ("HTTPS://CONTEST.EXAMPLE.COM:443/announcement?track=acm#old",
     "https://contest.example.com/announcement?track=acm#new"),
    ("HTTP://CONTEST.EXAMPLE.COM:80/announcement", "http://contest.example.com/announcement"),
    ("https://[2001:DB8::1]:443/path", "https://[2001:db8::1]/path#section"),
])
def test_url_equivalents_share_claim_and_original_source_is_preserved(db_file, original, equivalent):
    cid, _ = publish(url=original)
    assert publish(url=equivalent) == (cid, False)
    assert database.get_competition(cid)["official_site"] == original
    assert [source["url"] for source in database.list_sources(cid)] == [original]
    assert counts(db_file[0]) == (1, 1, 1, 1)


@pytest.mark.parametrize("other_url", [
    "http://contest.example.com/announcement?track=acm",
    "https://contest.example.com:444/announcement?track=acm",
    "https://contest.example.com/Announcement?track=acm",
    "https://contest.example.com/announcement/?track=acm",
    "https://contest.example.com/announcement?track=team",
    "https://contest.example.com/announcement?track=acm&extra=1",
])
def test_url_path_query_scheme_and_nondefault_port_remain_distinct(db_file, other_url):
    first, _ = publish()
    second, created = publish(url=other_url)
    assert created is True and first != second
    assert counts(db_file[0]) == (2, 2, 2, 2)


def test_empty_query_marker_is_preserved_in_identity(db_file):
    first, _ = publish(url="https://contest.example.com/path")
    second, created = publish(url="https://contest.example.com/path?")
    assert created is True and first != second


@pytest.mark.parametrize("changed", [{"edition": "2027"}, {"name": "另一项校赛"}])
def test_different_editions_and_names_at_same_url_are_separate(db_file, changed):
    first, _ = publish()
    second, created = publish({**INFO, **changed})
    assert created is True and first != second
    assert counts(db_file[0]) == (2, 2, 2, 2)


def test_known_edition_keeps_identity_when_dates_are_corrected(db_file):
    cid, _ = publish()
    assert publish({**INFO, "official_time": "2026-11-22 09:00"}) == (cid, False)
    assert database.get_competition(cid)["official_time"] == INFO["official_time"]


@pytest.mark.parametrize("changed", [
    {"official_reg_start": "2026-11-02 08:00"},
    {"official_reg_deadline": "2026-11-11 23:59"},
    {"official_time": "2026-11-21 09:00"},
    {"official_time": ""},
])
def test_missing_edition_keeps_different_times_or_ambiguous_identity_separate(db_file, changed):
    info = {**INFO, "edition": ""}
    first, _ = publish(info)
    second, created = publish({**info, **changed})
    assert created is True and first != second


def test_missing_edition_still_deduplicates_exact_repeats(db_file):
    info = {**INFO, "edition": ""}
    cid, _ = publish(info)
    assert publish(info) == (cid, False)
    assert counts(db_file[0]) == (1, 1, 1, 1)


@pytest.mark.parametrize("changed", [{"name": "另一项校赛"}, {"official_time": "2026-11-22 09:00"}])
def test_amended_fields_do_not_create_claims_for_another_name_or_session(db_file, changed):
    info = {**INFO, "edition": ""}
    first, _ = publish(info)
    database.update_competition(first, changed)
    second, created = publish({**info, **changed})
    assert created is True and second != first
    assert publish(info) == (first, False)


def test_claim_insert_failure_rolls_back_competition_source_and_baseline(db_file):
    with sqlite3.connect(db_file[0]) as conn:
        conn.execute("CREATE TRIGGER fail_claim BEFORE INSERT ON publication_claims "
                     "BEGIN SELECT RAISE(ABORT, 'claim write failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="claim write failure"):
        publish()
    assert counts(db_file[0]) == (0, 0, 0, 0)
    with sqlite3.connect(db_file[0]) as conn:
        conn.execute("DROP TRIGGER fail_claim")
    assert publish()[1] is True


def test_extraction_validator_and_both_mappers_preserve_optional_fields():
    raw = {
        "name": " 校赛 ", "edition": "2026", "reg_start": "2026/11/01 08:00",
        "reg_deadline": "2026-11-10 23:59", "contest_time": "2026-11-20 09:00",
        "raw_note": INFO["official_rules"], "tracks": INFO["tracks"],
        "device_req": INFO["device_req"], "summary": INFO["summary"],
    }
    cleaned, error = validate_contest_info(raw)
    assert error is None
    assert map_competition_info(cleaned) == INFO
    assert crawler._map_competition_info(cleaned) == INFO
    assert map_competition_info({"name": "校赛", "summary": "", "tracks": None}) == {"name": "校赛"}


def test_official_aliases_take_precedence_over_legacy_fields():
    cleaned, error = validate_contest_info({
        "name": "校赛", "official_reg_start": "2026-11-01",
        "official_reg_deadline": "2026-11-12 23:59", "reg_deadline": "2026-11-10 23:59",
        "official_time": "2026-11-22 09:00", "contest_time": "2026-11-20 09:00",
        "official_rules": "新版规则", "raw_note": "旧版规则",
    })
    assert error is None
    mapped = map_competition_info(cleaned)
    assert mapped["official_reg_start"] == "2026-11-01 00:00"
    assert mapped["official_reg_deadline"] == "2026-11-12 23:59"
    assert mapped["official_time"] == "2026-11-22 09:00"
    assert mapped["official_rules"] == "新版规则"


@pytest.mark.parametrize("info", [{}, {"name": ""}, {"name": 123}, {"name": None}])
def test_mapper_requires_name_for_each_complete_extraction(info):
    with pytest.raises(ValueError, match="比赛名称"):
        map_competition_info(info)


@pytest.mark.parametrize("field", ["edition", "tracks", "device_req", "summary", "reg_start",
                                  "official_reg_start", "official_reg_deadline", "official_time"])
def test_optional_field_types_are_validated_before_storage(field):
    cleaned, error = validate_contest_info({"name": "校赛", field: 123})
    assert cleaned is None and error


@pytest.mark.parametrize("official_aliases", [False, True])
async def test_onebot_publish_twice_preserves_fields_without_second_notification(db_file, monkeypatch, official_aliases):
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2", "g3"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    onebot = SimpleNamespace(send_group_msg=AsyncMock(return_value=True))
    bot = main.ACMBot(onebot)
    extracted = {
        "name": "完整校赛", "edition": "2026", "reg_start": "2026-11-01 08:00",
        "reg_deadline": "2026-11-10 23:59", "contest_time": "2026-11-20 09:00",
        "tracks": "团队赛", "raw_note": "规则原文", "device_req": "自带电脑",
        "summary": "官方摘要", "official_site": "https://invented.example.com/wrong",
    }
    if official_aliases:
        for legacy, official in (("reg_start", "official_reg_start"),
                                 ("reg_deadline", "official_reg_deadline"),
                                 ("contest_time", "official_time"), ("raw_note", "official_rules")):
            extracted[official] = extracted.pop(legacy)
    monkeypatch.setattr(main, "fetch_text", AsyncMock(return_value=PAGE))
    monkeypatch.setattr(llm, "_chat_once", AsyncMock(return_value=json.dumps(extracted, ensure_ascii=False)))

    def message(mid):
        return OneBotMessage({"post_type": "message", "message_type": "group", "group_id": "g1",
                              "user_id": "teacher", "message_id": mid, "raw_message": f"发布 {URL}"})

    await bot.on_group_at_message_create(message("first"))
    record = database.list_competitions(status="confirmed")[0]
    expected = {
        "edition": "2026", "official_reg_start": "2026-11-01 08:00",
        "official_reg_deadline": "2026-11-10 23:59", "official_time": "2026-11-20 09:00",
        "tracks": "团队赛", "official_rules": "规则原文", "device_req": "自带电脑",
        "summary": "官方摘要", "official_site": URL,
    }
    for field, value in expected.items():
        assert record[field] == value
    first_calls = list(onebot.send_group_msg.await_args_list)
    notices = [call.args for call in first_calls if "【新比赛发布】" in call.args[1]]
    assert [group for group, content in notices] == ["g1", "g2", "g3"]
    assert all(expected["official_reg_deadline"] in content and expected["official_time"] in content
               for group, content in notices)
    assert "已记录" in first_calls[0].args[1] and "群通知" in first_calls[-1].args[1]

    await bot.on_group_at_message_create(message("second"))
    assert database.list_competitions(status="confirmed") == [record]
    assert counts(db_file[0]) == (1, 1, 1, 1)
    added = onebot.send_group_msg.await_args_list[len(first_calls):]
    assert len(added) == 1 and added[0].args[0] == "g1" and "已存档" in added[0].args[1]
