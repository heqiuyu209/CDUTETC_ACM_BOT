"""平台比赛身份跨历史编号共享提醒成功和租约，原回执始终保留。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from cdutetc import config, database as db, group_cleanup, reminder


TZ = ZoneInfo("Asia/Shanghai")
SEED = datetime(2026, 11, 21, 9, 0, tzinfo=TZ)
NOW = SEED.replace(minute=30)
TARGET = "2026-11-21 10:00"
CF = "https://codeforces.com/contest/2288"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "identity_reminders.sqlite3"))
    monkeypatch.setattr(db, "_delivery_now", lambda value=None: value or SEED)
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_ENABLED", False)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_GROUPS", set())
    monkeypatch.setattr(config, "CONTEST_REMIND_OFFSETS_MINUTES", (30, 15))
    monkeypatch.setattr(config, "CONTEST_REMIND_GRACE_SECONDS", 120)
    monkeypatch.setattr(config, "REMIND_OFFSET_HOURS", (1,))
    db.init_db()


def _contest(name="Codeforces Round 1070 (Div. 2)", url=CF, target=TARGET, groups=("g1",)):
    cid = db.create_competition({"name": name, "official_site": url,
                                 "official_time": target, "official_reg_deadline": target}, status="confirmed")
    db.set_competition_notify_groups(cid, groups)
    return cid


def _pair(groups=("g1",)):
    return _contest(groups=groups), _contest("Codeforces Round (Div. 2)", groups=groups)


def _key(cid, kind="contest_start", offset=.5, group="g1", target=TARGET):
    return db.delivery_dedup_key(group, "competition", cid, kind, target, offset)


def _claim(cid, kind="contest_start", offset=.5, group="g1", now=NOW, target=TARGET, **kwargs):
    return db.claim_delivery(group, "competition", cid, kind, target, offset_hours=offset,
                             now=now, allowed_groups=set(config.ALLOWED_GROUPS), **kwargs)


def _success(cid, kind="contest_start", offset=.5, group="g1", now=NOW):
    token = _claim(cid, kind, offset, group, now)
    assert token
    key = _key(cid, kind, offset, group)
    assert db.record_delivery_success(key, token, now=now)
    assert db.finish_delivery(key, token, True, now=now)
    return key


def _receipts():
    with db._connect() as conn:
        return conn.execute("SELECT dedup_key,lease_token,sent_at FROM reminder_success_receipts ORDER BY dedup_key").fetchall()


async def _scan(kind, send, now=NOW):
    function = reminder.check_contest_start_reminders if kind == "contest_start" else reminder.check_general_reminders
    return await function(send, now=now)


@pytest.mark.parametrize("kind", ["contest_start", "reg_deadline"])
async def test_same_platform_node_across_historical_ids_sends_once(env, kind):
    first, second = _pair()
    send = AsyncMock(return_value=True)
    assert await _scan(kind, send) == 1
    send.assert_awaited_once()
    assert len(_receipts()) == 1
    assert await _scan(kind, send) == 0
    assert send.await_count == 1
    assert len(db.list_competitions()) == 2
    assert {first, second} == {item["id"] for item in db.list_competitions()}


@pytest.mark.parametrize("kind", ["contest_start", "reg_deadline"])
async def test_existing_success_blocks_duplicate_added_later_without_erasing_old_receipt(env, kind):
    first = _contest()
    send = AsyncMock(return_value=True)
    assert await _scan(kind, send) == 1
    before = _receipts()
    assert before
    second = _contest("Codeforces Round (Div. 2)")
    assert first != second
    assert await _scan(kind, send) == 0
    assert send.await_count == 1
    assert _receipts() == before


@pytest.mark.parametrize("kind,offset", [("contest_start", .5), ("reg_deadline", 1)])
def test_independent_success_receipt_survives_delivery_row_deletion_and_blocks_sibling(env, kind, offset):
    first, second = _pair()
    key = _success(first, kind, offset)
    before = _receipts()
    with db._connect() as conn:
        conn.execute("DELETE FROM reminder_deliveries WHERE dedup_key=?", (key,))
    assert _claim(second, kind, offset) is None
    assert _receipts() == before


@pytest.mark.parametrize("kind,offset", [("contest_start", .5), ("reg_deadline", 1)])
def test_old_sent_delivery_without_receipt_still_blocks_sibling(env, kind, offset):
    first, second = _pair()
    key = _success(first, kind, offset)
    with db._connect() as conn:
        conn.execute("DELETE FROM reminder_success_receipts WHERE dedup_key=?", (key,))
    assert _claim(second, kind, offset) is None
    assert db.get_delivery_by_key(key)["status"] == "sent"


@pytest.mark.parametrize("kind,offset", [("contest_start", .5), ("reg_deadline", 1)])
def test_active_sibling_lease_prevents_second_id_claim(env, kind, offset):
    first, second = _pair()
    token = _claim(first, kind, offset)
    assert token
    assert _claim(second, kind, offset) is None
    assert db.validate_delivery_claim(_key(first, kind, offset), token, now=NOW, allowed_groups={"g1"})
    assert db.get_delivery_by_key(_key(first, kind, offset))["lease_token"] == token


def test_concurrent_sqlite_claims_across_two_ids_allow_one_lease(env):
    first, second = _pair()
    ready = Barrier(2)

    def claim(cid):
        ready.wait(timeout=5)
        return _claim(cid)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, [first, second]))
    assert sum(token is not None for token in results) == 1


async def test_concurrent_scans_awaiting_send_cannot_claim_other_historical_id(env):
    _pair()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def send(group, text):
        calls.append((group, text))
        entered.set()
        await release.wait()
        return True

    task = asyncio.create_task(reminder.check_contest_start_reminders(send, now=NOW))
    await asyncio.wait_for(entered.wait(), timeout=3)
    try:
        assert await reminder.check_contest_start_reminders(send, now=NOW) == 0
    finally:
        release.set()
    assert await task == 1
    assert len(calls) == 1


@pytest.mark.parametrize("kind,offset", [("contest_start", .5), ("reg_deadline", 1)])
def test_expired_unsuccessful_sibling_lease_does_not_permanently_block_recovery(env, kind, offset):
    first, second = _pair()
    assert _claim(first, kind, offset, lease_seconds=30)
    assert _claim(second, kind, offset, now=NOW + timedelta(seconds=31))
    assert _receipts() == []


def test_failed_sibling_send_releases_identity_for_retry(env):
    first, second = _pair()
    token = _claim(first)
    assert token
    assert db.finish_delivery(_key(first), token, False, now=NOW, error="offline failure")
    assert _claim(second)
    assert _receipts() == []


@pytest.mark.parametrize("kind,offset,now", [
    ("contest_start", .5, NOW), ("contest_start", .25, NOW.replace(minute=45)),
    ("reg_deadline", 1, NOW), ("reg_deadline", 24, NOW),
])
def test_old_receipt_without_offset_covers_all_sibling_offsets(env, kind, offset, now):
    first, second = _pair()
    old_key = _key(first, kind, offset=None)
    with db._connect() as conn:
        conn.execute("INSERT INTO reminder_success_receipts(dedup_key,lease_token,sent_at) VALUES(?,?,?)",
                     (old_key, "old-success-token", NOW.strftime("%Y-%m-%d %H:%M:%S")))
    before = _receipts()
    assert _claim(second, kind, offset, now=now) is None
    assert _receipts() == before


def test_another_start_offset_still_sends_once_for_platform_event(env):
    _pair()
    send = AsyncMock(return_value=True)
    assert asyncio.run(reminder.check_contest_start_reminders(send, now=NOW)) == 1
    first = _receipts()
    assert asyncio.run(reminder.check_contest_start_reminders(send, now=NOW.replace(minute=45))) == 1
    assert send.await_count == 2
    assert len(_receipts()) == 2
    assert all(item in _receipts() for item in first)
    assert {row[0].rsplit("|", 1)[1] for row in _receipts()} == {"0.5", "0.25"}


def test_another_deadline_offset_remains_independent(env):
    first, second = _pair()
    _success(first, "reg_deadline", 24)
    before = _receipts()
    assert _claim(second, "reg_deadline", 1)
    assert _receipts() == before


@pytest.mark.parametrize("kind,offset", [("contest_start", .5), ("reg_deadline", 1)])
def test_other_group_can_claim_same_platform_and_node(env, kind, offset):
    first, second = _pair(groups=("g1", "g2"))
    _success(first, kind, offset, group="g1")
    assert _claim(second, kind, offset, group="g2")


def test_reminder_types_remain_independent_for_same_platform(env):
    first, second = _pair()
    _success(first)
    assert _claim(second, "reg_deadline", 1)


@pytest.mark.parametrize("url", [
    "https://codeforces.com/contest/2287", "https://codeforces.com/gym/2288",
    "https://ac.nowcoder.com/acm/contest/2288", "https://example.com/contest/2288",
])
def test_different_platform_identity_does_not_share_success(env, url):
    first = _contest()
    _success(first)
    second = _contest("同时间其他比赛", url=url)
    assert _claim(second)


@pytest.mark.parametrize("url", ["https://example.com/contest/2288", ""])
def test_unknown_identity_keeps_existing_per_object_delivery_semantics(env, url):
    first = _contest(url=url)
    second = _contest("同名历史记录", url=url)
    _success(first)
    assert _claim(second)


def test_new_target_time_after_reschedule_is_not_blocked_by_old_success(env):
    first, second = _pair()
    _success(first)
    before = _receipts()
    assert db.amend_competition(second, {"official_time": "2026-11-21 11:00"}, operator="admin")
    token = _claim(second, now=NOW.replace(hour=10), target="2026-11-21 11:00")
    assert token
    assert _receipts() == before


def test_success_identity_stays_with_original_platform_when_link_is_changed(env):
    first, second = _pair()
    _success(first)
    assert db.amend_competition(first, {"official_site": "https://codeforces.com/contest/9999"}, operator="admin")
    assert _claim(second) is None
    unrelated = _contest("真正的9999比赛", url="https://codeforces.com/contest/9999")
    assert _claim(unrelated)


def test_sibling_success_blocks_claim_even_if_original_record_is_archived(env):
    first, second = _pair()
    _success(first)
    before = _receipts()
    assert group_cleanup.archive_competition("g1", first, operator="admin")
    assert _claim(second) is None
    assert _receipts() == before


def test_canonical_time_formats_share_sibling_receipt(env):
    first, second = _pair()
    _success(first)
    assert _claim(second, target="2026/11/21 10:00") is None
    assert _claim(second, target="2026-11-21 10:00:00") is None


def test_validate_claim_rechecks_sibling_success_recorded_after_claim(env):
    first, second = _pair()
    token = _claim(second)
    assert token
    with db._connect() as conn:
        conn.execute("INSERT INTO reminder_success_receipts(dedup_key,lease_token,sent_at) VALUES(?,?,?)",
                     (_key(first), "other-success", NOW.strftime("%Y-%m-%d %H:%M:%S")))
    before = _receipts()
    assert not db.validate_delivery_claim(_key(second), token, now=NOW, allowed_groups={"g1"})
    assert _receipts() == before


async def test_nonviable_historical_node_cannot_hide_valid_sibling(env):
    first, second = _pair()
    with db._connect() as conn:
        conn.execute("UPDATE reminder_deliveries SET status='cancelled' WHERE obj_id=?", (first,))
    send = AsyncMock(return_value=True)
    assert await reminder.check_contest_start_reminders(send, now=NOW) == 1
    send.assert_awaited_once()
    assert _receipts()[0][0].split("|")[2] == str(second)
