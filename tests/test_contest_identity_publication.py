"""稳定平台赛事身份贯穿发布、审批与旧持久记录；真实临时 SQLite。"""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier
from zoneinfo import ZoneInfo

import pytest

from cdutetc import config, database as db
from cdutetc.db import connection, training

NOW = datetime(2026, 11, 21, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
TIME = "2026-11-21 22:35"
CF = "https://codeforces.com/contest/2288"
NC = "https://ac.nowcoder.com/acm/contest/99901"


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "contest-identity.sqlite3"
    connections = []

    def connect():
        conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
        connections.append(conn)
        return conn

    monkeypatch.setattr(connection, "_connect", connect)
    monkeypatch.setattr(connection, "DB_PATH", str(path))
    monkeypatch.setattr(connection, "now_str", lambda: "2026-11-21 09:00:00")
    monkeypatch.setattr(training, "_delivery_now", lambda value=None: value or NOW)
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "PUBLISH_SCOPE_TO_SOURCE", True)
    monkeypatch.setattr(config, "CONTEST_REMIND_OFFSETS_MINUTES", (30, 15))
    db.init_db()
    yield path
    for conn in connections:
        conn.close()


def _info(url=CF, **changes):
    return {"name": "Codeforces Round 1136 (Div. 2)", "edition": "Codeforces",
            "official_time": TIME, "official_site": url, "summary": "首个完整来源", **changes}


def _publish(info, url=None, source_text="首个来源正文", group="g1"):
    return db.create_published_competition_once(
        info, info["official_site"] if url is None else url, source_text,
        operator="publish:teacher", source_group_id=group,
    )


def _pending(info, url=None, group="g1"):
    return db.create_pending_publication(
        info, group, info["official_site"] if url is None else url, "待审来源正文", "teacher",
    )


def _business_snapshot(cid):
    with db._connect() as conn:
        deliveries = conn.execute("SELECT * FROM reminder_deliveries WHERE obj_type='competition' AND obj_id=? ORDER BY id", (cid,)).fetchall()
        scopes = conn.execute("SELECT * FROM competition_notify_groups WHERE competition_id=? ORDER BY group_id", (cid,)).fetchall()
    return (db.get_competition(cid), db.list_competition_versions(cid),
            db.list_sources(cid, active_only=False), deliveries, scopes)


@pytest.mark.parametrize("url", [CF, NC])
def test_stable_contest_link_ignores_title_edition_and_time_variants(env, url):
    cid, created = _publish(_info(url))
    assert created
    before = _business_snapshot(cid)
    variant = _info(url, name="同一比赛的另一种标题", edition="", official_time="2026-11-22 22:35")
    assert _publish(variant, source_text="后抓到的正文") == (cid, False)
    assert _business_snapshot(cid) == before
    assert len(db.list_competitions()) == 1


@pytest.mark.parametrize("original,variant", [
    (CF, CF + "?locale=en#registration"),
    (CF, CF + "/"),
    (NC, NC + "?from=home#rank"),
    (NC, NC + "/"),
])
def test_known_platform_link_variants_preserve_first_source_and_baseline(env, original, variant):
    cid, _ = _publish(_info(original))
    before = _business_snapshot(cid)
    assert _publish(_info(variant, name="标题变体", edition="")) == (cid, False)
    assert _business_snapshot(cid) == before
    source = db.list_sources(cid)[0]
    assert source["url"] == original
    assert source["body_hash"] == hashlib.sha256("首个来源正文".encode()).hexdigest()


@pytest.mark.parametrize("other_url", ["https://codeforces.com/contest/2287", "https://ac.nowcoder.com/acm/contest/99902"])
def test_different_platform_ids_with_same_title_and_start_remain_separate(env, other_url):
    cid, _ = _publish(_info())
    other, created = _publish(_info(other_url))
    assert created and other != cid
    assert len(db.list_competitions()) == 2
    assert {item["id"] for item in db.list_group_competitions("g1")} == {cid, other}


def test_cf_division_ids_are_preserved_even_when_names_match(env):
    first = _info("https://codeforces.com/contest/2287", name="Codeforces Round 1136", edition="")
    second = _info("https://codeforces.com/contest/2288", name="Codeforces Round 1136", edition="")
    a, _ = _publish(first)
    b, created = _publish(second)
    assert created and a != b
    assert len(db.list_competitions()) == 2


def test_cf_gym_and_contest_namespace_do_not_share_the_same_numeric_id(env):
    cid, _ = _publish(_info(CF))
    gym, created = _publish(_info("https://codeforces.com/gym/2288"))
    assert created and gym != cid
    assert len(db.list_competitions()) == 2


@pytest.mark.parametrize("first,second", [
    ("https://codeforces.com/contest/2287", "https://codeforces.com/contest/2288"),
    ("https://ac.nowcoder.com/acm/contest/99901", "https://ac.nowcoder.com/acm/contest/99902"),
    ("https://codeforces.com/contest/99901", "https://ac.nowcoder.com/acm/contest/99901"),
])
def test_pending_approval_does_not_merge_different_ids_or_platforms(env, first, second):
    a, created_a = db.approve_pending_publication(_pending(_info(first)), "admin")
    b, created_b = db.approve_pending_publication(_pending(_info(second)), "admin")
    assert created_a and created_b and a != b
    assert len(db.list_competitions()) == 2


@pytest.mark.parametrize("url", [CF, NC])
def test_direct_publish_then_pending_title_variant_reuses_same_record(env, url):
    cid, _ = _publish(_info(url))
    before = _business_snapshot(cid)
    pid = _pending(_info(url, name="不带轮次的比赛标题", edition=""))
    assert db.approve_pending_publication(pid, "admin") == (cid, False)
    assert db.get_pending_publication(pid)["status"] == "approved"
    assert _business_snapshot(cid) == before
    assert len(db.list_competitions()) == 1


@pytest.mark.parametrize("url", [CF, NC])
def test_pending_approval_then_direct_title_variant_reuses_same_record(env, url):
    pid = _pending(_info(url))
    cid, created = db.approve_pending_publication(pid, "admin")
    assert created
    before = _business_snapshot(cid)
    assert _publish(_info(url, name="去掉轮次的比赛标题", edition=""), source_text="重复正文") == (cid, False)
    assert _business_snapshot(cid) == before
    assert len(db.list_competitions()) == 1


@pytest.mark.parametrize("via_pending", [False, True])
def test_rescheduled_contest_duplicate_does_not_restore_old_time_name_or_source(env, via_pending):
    info = _info(edition="")
    cid, _ = db.approve_pending_publication(_pending(info), "admin") if via_pending else _publish(info)
    db.update_competition(cid, {"name": "手工确认标题", "official_time": "2026-11-23 20:00", "summary": "已修正资料"})
    sid = db.list_sources(cid)[0]["id"]
    db.update_source_fetch(sid, etag='"confirmed-etag"', body_hash="confirmed-body", last_status="nochange")
    before = _business_snapshot(cid)
    variant = _info(name="新一轮抓取标题", edition="")
    result = db.approve_pending_publication(_pending(variant), "admin") if via_pending else _publish(variant)
    assert result == (cid, False)
    assert _business_snapshot(cid) == before
    assert len(db.list_competitions()) == 1


def test_reapproving_same_pending_id_after_manual_reschedule_finds_original_record(env):
    pid = _pending(_info(edition=""))
    cid, _ = db.approve_pending_publication(pid, "admin")
    db.update_competition(cid, {"name": "手工改标题", "official_time": "2026-11-23 20:00"})
    before = _business_snapshot(cid)
    assert db.approve_pending_publication(pid, "admin") == (cid, False)
    assert _business_snapshot(cid) == before
    assert len(db.list_competitions()) == 1


@pytest.mark.parametrize("mode", ["direct", "pending", "mixed"])
def test_independent_connections_create_one_record_for_same_stable_id(env, mode):
    gate = Barrier(2)
    infos = [_info(), _info(name="同场短标题", edition="")]
    pending_ids = [_pending(item) for item in infos] if mode != "direct" else []

    def worker(index):
        gate.wait(timeout=10)
        if mode == "pending" or mode == "mixed" and index == 1:
            return db.approve_pending_publication(pending_ids[index], "admin")
        return _publish(infos[index])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=15) for future in [pool.submit(worker, 0), pool.submit(worker, 1)]]
    assert results[0][0] == results[1][0]
    assert sorted(created for _, created in results) == [False, True]
    assert len(db.list_competitions()) == 1
    assert len(db.list_competition_versions(results[0][0])) == 1


@pytest.mark.parametrize("amend_site", [False, True])
def test_legacy_persistent_claim_is_compatible_with_stable_identity_after_amendments(env, amend_site):
    info = _info(edition="")
    cid = db.create_competition(info, status="confirmed")
    sid = db.add_competition_source(cid, CF)
    db.update_source_fetch(sid, body_hash="legacy-success-body", last_status="nochange")
    old_key = json.dumps([CF, info["name"], "", "", "", TIME], ensure_ascii=False, separators=(",", ":"))
    with db._connect() as conn:
        conn.execute("INSERT INTO publication_claims(identity_key,competition_id,created_at) VALUES(?,?,?)", (old_key, cid, "2026-11-20 08:00:00"))
    changes = {"name": "旧库人工标题", "official_time": "2026-11-23 20:00"}
    if amend_site:
        changes["official_site"] = "https://announcement.example.com/manual"
    db.update_competition(cid, changes)
    before = _business_snapshot(cid)
    db.init_db()
    assert _publish(_info(name="新的提取标题", edition="")) == (cid, False)
    assert _business_snapshot(cid) == before
    with db._connect() as conn:
        assert conn.execute("SELECT competition_id FROM publication_claims WHERE identity_key=?", (old_key,)).fetchone() == (cid,)


def test_old_persistent_claim_alone_preserves_identity_when_all_editable_urls_changed(env):
    generic = "https://announcement.example.com/manual"
    info = _info(generic, name="人工保留的旧比赛", edition="")
    cid = db.create_competition(info, status="confirmed")
    old_key = json.dumps([CF, "Codeforces Round 1136 (Div. 2)", "", "", "", TIME], ensure_ascii=False, separators=(",", ":"))
    with db._connect() as conn:
        conn.execute("INSERT INTO publication_claims(identity_key,competition_id,created_at) VALUES(?,?,?)", (old_key, cid, "2026-11-20 08:00:00"))
    before = _business_snapshot(cid)
    assert _publish(_info(name="抓取后新的标题", edition="")) == (cid, False)
    assert _business_snapshot(cid) == before
    assert len(db.list_competitions()) == 1


def test_source_url_authority_wins_over_model_supplied_conflicting_platform_link(env):
    cid, _ = _publish(_info(CF))
    before = _business_snapshot(cid)
    variant = _info("https://codeforces.com/contest/2287", name="模型标题变体", edition="")
    assert _publish(variant, url=CF) == (cid, False)
    assert _business_snapshot(cid) == before
    assert len(db.list_competitions()) == 1


@pytest.mark.parametrize("evidence", ["official_site", "source", "original_version"])
def test_unclaimed_legacy_record_can_be_found_by_platform_id_without_exact_title(env, evidence):
    original = _info()
    if evidence == "source":
        original["official_site"] = "https://announcement.example.com/manual"
    cid = db.create_competition(original, status="confirmed")
    if evidence == "source":
        db.add_competition_source(cid, CF)
    elif evidence == "original_version":
        db.update_competition(cid, {"official_site": "https://announcement.example.com/manual"})
    db.update_competition(cid, {"name": "旧库改名", "official_time": "2026-11-23 20:00"})
    before = _business_snapshot(cid)
    assert _publish(_info(name="新的标题", edition="")) == (cid, False)
    assert _business_snapshot(cid) == before
    assert len(db.list_competitions()) == 1


@pytest.mark.parametrize("url", ["https://codeforces.com.evil.example/contest/2288", "https://ac.nowcoder.com.evil.example/acm/contest/99901", "https://event.example.com/contest/2288"])
def test_unrecognized_hosts_keep_exact_publication_identity(env, url):
    cid, _ = _publish(_info(url, edition="2026"))
    other, created = _publish(_info(url, edition="2026", name="另一场比赛"))
    assert created and other != cid
    assert len(db.list_competitions()) == 2


def test_stable_duplicate_does_not_expand_direct_source_group_scope(env):
    cid, _ = _publish(_info(), group="g1")
    before = _business_snapshot(cid)
    assert _publish(_info(name="同场另名", edition=""), group="g2") == (cid, False)
    assert db.get_competition_target_groups(cid) == {"g1"}
    assert _business_snapshot(cid) == before


def test_duplicate_preserves_sent_and_pending_reminder_nodes(env):
    cid, _ = _publish(_info())
    with db._connect() as conn:
        node = conn.execute("SELECT id,dedup_key FROM reminder_deliveries WHERE obj_id=? ORDER BY id LIMIT 1", (cid,)).fetchone()
        assert node is not None
        conn.execute("UPDATE reminder_deliveries SET status='sent',sent_at='2026-11-21 22:00:00' WHERE id=?", (node[0],))
        conn.execute("INSERT INTO reminder_success_receipts(dedup_key,lease_token,sent_at) VALUES(?,'old-token','2026-11-21 22:00:00')", (node[1],))
    before = _business_snapshot(cid)
    assert _publish(_info(name="另一标题", edition="")) == (cid, False)
    assert _business_snapshot(cid) == before
    with db._connect() as conn:
        assert conn.execute("SELECT lease_token FROM reminder_success_receipts WHERE dedup_key=?", (node[1],)).fetchone() == ("old-token",)


@pytest.mark.parametrize("evidence", ["claim", "original_version", "source"])
@pytest.mark.parametrize("modified_url", ["", "https://codeforces.com/contest/2287", NC])
def test_modified_or_cleared_official_site_cannot_claim_a_different_platform_identity(env, evidence, modified_url):
    if evidence == "claim":
        cid, _ = _publish(_info())
    elif evidence == "original_version":
        cid = db.create_competition(_info(), status="confirmed")
    else:
        cid = db.create_competition(_info("https://announcement.example.com/original"), status="confirmed")
        db.add_competition_source(cid, CF)
    db.update_competition(cid, {"official_site": modified_url, "name": "人工标题"})
    before = _business_snapshot(cid)
    if modified_url:
        other, created = _publish(_info(modified_url, name="另一赛事"))
        assert created and other != cid
        assert _business_snapshot(cid) == before
    assert _publish(_info(name="旧场标题变体", edition="")) == (cid, False)
    assert _business_snapshot(cid) == before


@pytest.mark.parametrize("url", [CF, NC])
@pytest.mark.parametrize("via_pending", [False, True])
def test_official_site_alone_deduplicates_known_id_without_registering_empty_source(env, url, via_pending):
    original = _info(url)
    if via_pending:
        cid, created = db.approve_pending_publication(_pending(original, url=""), "admin")
    else:
        cid, created = _publish(original, url="")
    assert created and not db.list_sources(cid)
    before = _business_snapshot(cid)
    variant = _info(url, name="仅官网字段的新标题", edition="", official_time="2026-11-23 20:00")
    if via_pending:
        result = db.approve_pending_publication(_pending(variant, url=""), "admin")
    else:
        result = _publish(variant, url="")
    assert result == (cid, False)
    assert _business_snapshot(cid) == before


def test_pending_source_url_authority_wins_over_model_supplied_other_contest_id(env):
    cid, _ = _publish(_info())
    before = _business_snapshot(cid)
    variant = _info("https://codeforces.com/contest/2287", name="模型错填的标题", edition="")
    pid = _pending(variant, url=CF)
    assert db.approve_pending_publication(pid, "admin") == (cid, False)
    assert db.approve_pending_publication(pid, "admin") == (cid, False)
    assert _business_snapshot(cid) == before
    assert len(db.list_competitions()) == 1


def test_reapproving_same_pid_keeps_audited_record_when_fuller_duplicate_appears(env):
    pid = _pending(_info(name="Codeforces (Div. 2)", edition=""))
    cid, _ = db.approve_pending_publication(pid, "admin")
    db.update_competition(cid, {"name": "人工短标题", "official_time": "2026-11-23 20:00"})
    duplicate = db.create_competition(_info(name="Codeforces Round 1136 (Div. 2)", edition=""), status="confirmed")
    before = _business_snapshot(cid)
    assert db.approve_pending_publication(pid, "admin") == (cid, False)
    assert duplicate != cid and _business_snapshot(cid) == before


def test_reapproving_deleted_audited_record_does_not_redirect_to_another_duplicate(env):
    pid = _pending(_info(edition=""))
    cid, _ = db.approve_pending_publication(pid, "admin")
    duplicate = db.create_competition(_info(edition=""), status="confirmed")
    with db._connect() as conn:
        conn.execute("DELETE FROM competitions WHERE id=?", (cid,))
    before = _business_snapshot(duplicate)
    with pytest.raises(ValueError, match="对应比赛记录不存在"):
        db.approve_pending_publication(pid, "admin")
    assert _business_snapshot(duplicate) == before


def test_old_approved_pid_without_audit_does_not_match_same_fields_of_different_known_id(env):
    pid = _pending(_info(edition=""))
    cid, _ = db.approve_pending_publication(pid, "admin")
    other = db.create_competition(_info("https://codeforces.com/contest/2287", edition=""), status="confirmed")
    with db._connect() as conn:
        conn.execute("DELETE FROM audit_log WHERE action='approve_publication' AND object_id=?", (str(pid),))
        conn.execute("DELETE FROM competitions WHERE id=?", (cid,))
    before = _business_snapshot(other)
    with pytest.raises(ValueError, match="对应比赛记录不存在"):
        db.approve_pending_publication(pid, "admin")
    assert _business_snapshot(other) == before
