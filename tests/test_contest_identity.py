"""稳定赛事编号的可信地址边界与抓取写入事务。"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from cdutetc import config, contest_identity, database as db
from cdutetc.db import connection


@pytest.mark.parametrize("url,key", [
    ("https://codeforces.com/contest/2288", "codeforces:contest:2288"),
    ("HTTP://WWW.CODEFORCES.COM:80/contest/2288/?locale=zh#rank", "codeforces:contest:2288"),
    ("https://codeforces.com/contest/2288/problem/A", "codeforces:contest:2288"),
    ("https://codeforces.com/gym/2288", "codeforces:gym:2288"),
    ("https://ac.nowcoder.com/acm/contest/144178/rank?from=calendar", "nowcoder:contest:144178"),
    ("https://codeforces.com:443/contest/2288", "codeforces:contest:2288"),
])
def test_platform_identity_recognizes_only_exact_official_contest_paths(url, key):
    assert contest_identity.platform_contest_key(url) == key


@pytest.mark.parametrize("url", [
    None, "", "https://codeforces.com.evil.example/contest/2288",
    "https://evilcodeforces.com/contest/2288", "https://codeforces.com@evil.example/contest/2288",
    "https://user@codeforces.com/contest/2288", "https://codeforces.com:444/contest/2288",
    "https://codeforces.com:80/contest/2288", "http://codeforces.com:443/contest/2288",
    "https://codeforces.com:bad/contest/2288", "ftp://codeforces.com/contest/2288",
    "https://codeforces.com/contests", "https://codeforces.com/contest/2288suffix",
    "https://codeforces.com/contest/0", "https://ac.nowcoder.com/acm/contest/144178suffix",
])
def test_ambiguous_or_unrelated_urls_do_not_acquire_platform_identity(url):
    assert contest_identity.platform_contest_key(url) == ""


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "atomic-crawl.sqlite3"
    monkeypatch.setattr(connection, "DB_PATH", str(path))
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2"})
    db.init_db()
    return path


def _info(name="Codeforces Round 1136 (Div. 2)"):
    return {"name": name, "official_site": "https://codeforces.com/contest/2288",
            "official_time": "2030-11-21 22:35", "edition": "Codeforces"}


def _snapshot(path):
    with sqlite3.connect(path) as conn:
        return {table: conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                for table in ("competitions", "competition_versions", "competition_notify_groups",
                              "reminder_deliveries", "publication_claims", "sqlite_sequence")}


@pytest.mark.parametrize("table", ["competition_versions", "competition_notify_groups", "reminder_deliveries", "publication_claims"])
def test_crawl_creation_rolls_back_all_data_if_any_dependent_write_fails(env, table):
    with sqlite3.connect(env) as conn:
        conn.execute(f"CREATE TRIGGER offline_fail BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'offline failure'); END")
    before = _snapshot(env)
    with pytest.raises(sqlite3.IntegrityError, match="offline failure"):
        db.create_crawled_competition(_info(), "confirmed", notify_groups={"g1"})
    assert _snapshot(env) == before


def test_two_crawlers_claim_one_stable_identity_across_connections(env):
    barrier = Barrier(2)

    def create(index):
        barrier.wait(timeout=10)
        return db.create_crawled_competition(_info(f"同场标题{index}"), "confirmed", notify_groups={"g1"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(create, index) for index in range(2)]
        results = [future.result(timeout=15) for future in futures]
    assert results[0][0] == results[1][0]
    assert sorted(created for _, created in results) == [False, True]
    assert len(db.list_competitions()) == 1


def test_crawler_and_direct_publisher_share_platform_identity(env):
    cid, _ = db.create_crawled_competition(_info(), "confirmed", notify_groups={"g1"})
    before = _snapshot(env)
    assert db.create_published_competition_once(_info("短标题"), _info()["official_site"], "新的正文") == (cid, False)
    assert _snapshot(env) == before
