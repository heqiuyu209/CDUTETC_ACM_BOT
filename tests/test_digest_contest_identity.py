"""历史重复比赛仅在晨报展示去重，先复核群范围与投递再选择真实记录。"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from cdutetc import business_followup, config, daily_digest, database as db, group_cleanup
from cdutetc.errors import DeliveryCode


NOW = datetime(2026, 11, 21, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
CF = "https://codeforces.com/contest/2288"
FULL_TITLE = "Codeforces Round 1070 (Div. 2)"
SHORT_TITLE = "Codeforces Round (Div. 2)"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "digest_identity.sqlite3"))
    monkeypatch.setattr(db, "_delivery_now", lambda value=None: value or NOW)
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "ENV_ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "SUPER_ADMIN", {"admin"})
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_ENABLED", False)
    monkeypatch.setattr(config, "WEEKLY_CONTEST_GROUPS", set())
    monkeypatch.setattr(config, "CONTEST_REMIND_OFFSETS_MINUTES", (30, 15))
    monkeypatch.setattr(config, "REMIND_OFFSET_HOURS", (24, 1))
    db.init_db()


def _contest(name=FULL_TITLE, url=CF, when="2026-11-21 19:00", groups=("g1", "g2"), status="confirmed"):
    # 原始创建 API 可复现升级前已存在的同URL历史记录，不经过本次新入库去重。
    cid = db.create_competition({"name": name, "official_site": url, "official_time": when}, status=status)
    db.set_competition_notify_groups(cid, groups)
    return cid


def _ids(group="g1", now=NOW):
    return [item["id"] for item in daily_digest.today_competitions(group, now)]


def _cancel_nodes(cid, group="g1"):
    with db._connect() as conn:
        conn.execute("UPDATE reminder_deliveries SET status='cancelled'"
                     " WHERE group_id=? AND obj_type='competition' AND obj_id=?", (group, cid))


def test_screenshot_five_historical_rows_become_three_real_events(env):
    nc = _contest("牛客周末挑战赛", "https://ac.nowcoder.com/acm/contest/144178")
    cf2288 = _contest()
    _contest(SHORT_TITLE)
    cf2287 = _contest("Codeforces Round 1069 (Div. 1)", "https://codeforces.com/contest/2287")
    _contest("Codeforces Round (Div. 1)", "https://codeforces.com/contest/2287")
    detail = daily_digest.collect_daily_digest("g1", NOW)
    assert {item["id"] for item in detail["competitions"]} == {nc, cf2288, cf2287}
    assert detail["today_contest_count"] == 3
    assert "今日赛事（3 场）" in detail["text"]
    assert detail["text"].count("codeforces.com/contest/2288") == 1
    assert detail["text"].count("codeforces.com/contest/2287") == 1
    assert len(db.list_competitions()) == 5


@pytest.mark.parametrize("full_first", [True, False])
def test_complete_round_title_wins_regardless_of_creation_order(env, full_first):
    if full_first:
        full = _contest()
        _contest(SHORT_TITLE)
    else:
        _contest(SHORT_TITLE)
        full = _contest()
    items = daily_digest.today_competitions("g1", NOW)
    assert [item["id"] for item in items] == [full]
    assert items[0]["name"] == FULL_TITLE


@pytest.mark.parametrize("alias", [
    "https://www.codeforces.com/contest/2288?locale=zh#overview",
    "http://codeforces.com/contest/2288/standings",
    "https://codeforces.com:443/contest/2288/problem/A",
])
def test_platform_url_aliases_share_display_identity(env, alias):
    full = _contest()
    _contest(SHORT_TITLE, alias)
    assert _ids() == [full]


def test_nowcoder_tabs_share_one_display_event(env):
    full = _contest("牛客周末挑战赛 144178", "https://ac.nowcoder.com/acm/contest/144178")
    _contest("牛客周末挑战赛", "https://ac.nowcoder.com/acm/contest/144178#question")
    _contest("牛客周末挑战赛", "https://ac.nowcoder.com/acm/contest/144178/A")
    assert _ids() == [full]


def test_separate_cf_divisions_and_gym_ids_remain_separate(env):
    div1 = _contest("Codeforces Round 1070 (Div. 1)", "https://codeforces.com/contest/2287")
    div2 = _contest()
    gym = _contest("Codeforces Gym 2288", "https://codeforces.com/gym/2288")
    assert set(_ids()) == {div1, div2, gym}


@pytest.mark.parametrize("url", ["", "https://example.com/contest/2288", "https://codeforces.com.evil.test/contest/2288"])
def test_unknown_platform_does_not_merge_same_title_or_url(env, url):
    first = _contest(FULL_TITLE, url)
    second = _contest(FULL_TITLE, url)
    assert set(_ids()) == {first, second}


def test_same_title_and_time_with_distinct_platform_ids_remain_separate(env):
    first = _contest(SHORT_TITLE, "https://codeforces.com/contest/2287")
    second = _contest(SHORT_TITLE)
    assert set(_ids()) == {first, second}


def test_confirmed_group_visible_record_beats_better_title_outside_group(env):
    short = _contest(SHORT_TITLE, groups=("g1",))
    full = _contest(groups=("g2",))
    assert _ids("g1") == [short]
    assert _ids("g2") == [full]


def test_archived_better_title_is_filtered_before_display_dedup(env):
    short = _contest(SHORT_TITLE)
    full = _contest()
    assert group_cleanup.archive_competition("g1", full, operator="admin")
    assert _ids("g1") == [short]
    assert _ids("g2") == [full]
    assert group_cleanup.is_competition_archived("g1", full)


def test_draft_better_title_does_not_hide_confirmed_duplicate(env):
    short = _contest(SHORT_TITLE)
    _contest(status="draft")
    assert _ids() == [short]


def test_delivery_readiness_is_checked_before_better_title_selection(env):
    short = _contest(SHORT_TITLE)
    full = _contest()
    _cancel_nodes(full)
    assert _ids("g1") == [short]
    assert _ids("g2") == [full]


def test_group_delivery_block_still_suppresses_all_candidates(env):
    _contest()
    _contest(SHORT_TITLE)
    db.block_group_notifications("qqbot", "g1", DeliveryCode.QUOTA_EXHAUSTED.value, 11253)
    assert _ids("g1") == []
    assert len(_ids("g2")) == 1


def test_dates_are_filtered_before_dedup_and_do_not_replace_today_with_tomorrow(env):
    today = _contest(SHORT_TITLE)
    _contest(when="2026-11-22 19:00")
    assert _ids() == [today]


def test_chosen_record_keeps_its_own_time_location_and_metadata(env):
    short = _contest(SHORT_TITLE, when="2026-11-21 18:00")
    full = _contest(when="2026-11-21 19:00")
    other = _contest("牛客比赛", "https://ac.nowcoder.com/acm/contest/144178", when="2026-11-21 18:30")
    assert business_followup.set_competition_location("g1", "teacher", short, "SHORT_ROOM", now=NOW)
    assert business_followup.set_competition_location("g1", "teacher", full, "FULL_ROOM", now=NOW)
    items = daily_digest.today_competitions("g1", NOW)
    assert [item["id"] for item in items] == [other, full]
    chosen = items[1]
    assert chosen["start"] == "2026-11-21 19:00"
    assert chosen["start_dt"].hour == 19
    assert chosen["location"] == "FULL_ROOM"
    assert chosen["name"] == FULL_TITLE
    assert "SHORT_ROOM" not in daily_digest.collect_daily_digest("g1", NOW)["text"]


def test_group_overlays_remain_group_specific_after_dedup(env):
    full = _contest()
    _contest(SHORT_TITLE)
    assert business_followup.set_competition_location("g1", "teacher", full, "G1_ROOM", now=NOW)
    assert business_followup.set_competition_location("g2", "teacher", full, "G2_ROOM", now=NOW)
    first = daily_digest.today_competitions("g1", NOW)
    second = daily_digest.today_competitions("g2", NOW)
    assert len(first) == len(second) == 1
    assert first[0]["location"] == "G1_ROOM"
    assert second[0]["location"] == "G2_ROOM"


def test_digest_reference_is_bound_to_selected_real_id_and_only_updates_it(env):
    full = _contest()
    short = _contest(SHORT_TITLE)
    detail = daily_digest.collect_daily_digest("g1", NOW)
    business_followup.remember_digest("g1", "deduplicated-digest", detail, now=NOW)
    with db._connect() as conn:
        raw = conn.execute("SELECT items_json FROM business_message_context WHERE group_id='g1'"
                           " AND message_id='deduplicated-digest'").fetchone()[0]
    assert [item["id"] for item in json.loads(raw)] == [full]
    result = business_followup.handle_location("地点在B302", "g1", "teacher", msg_id="reply",
                                               reference_id="deduplicated-digest", now=NOW)
    assert result["changed"] is True
    locations = {item["id"]: item["location"] for item in db.list_group_competitions("g1")}
    assert locations[full] == "B302" and locations[short] == ""
    assert len(db.list_competitions()) == 2


@pytest.mark.parametrize("passive", [False, True])
def test_upcoming_candidates_use_same_identity_without_changing_passive_readiness(passive, env):
    full = _contest(when="2026-11-21 10:00")
    _contest(SHORT_TITLE, when="2026-11-21 10:00")
    if passive:
        _cancel_nodes(full)
    items = daily_digest.soon_competitions("g1", NOW, require_delivery_ready=not passive)
    assert [item["id"] for item in items] == [full]
    assert FULL_TITLE in daily_digest.build_piggyback_notice("g1", NOW)


def test_today_render_is_readonly_and_leaves_history_versions_and_delivery_rows(env):
    _contest()
    _contest(SHORT_TITLE)
    with db._connect() as conn:
        tables = ("competitions", "competition_versions", "competition_notify_groups", "reminder_deliveries")
        before = {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    first = daily_digest.collect_daily_digest("g1", NOW)
    second = daily_digest.collect_daily_digest("g1", NOW)
    assert first == second and len(first["competitions"]) == 1
    with db._connect() as conn:
        after = {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    assert before == after
