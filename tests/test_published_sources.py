"""发布来源登记与后续监控回归：真实临时 SQLite，抓取和 QQ 发送均离线。"""

import hashlib
import sqlite3
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import config
import crawler
import database
import main
from fetcher import FetchError
from onebot_client import OneBotMessage


URL = "https://contest.example.com/announcement"
PAGE = "  校赛公告\n报名截止 2026-11-10 23:59\n"
PUBLISHED_AT = "2026-11-01 08:00:00"
TZ = ZoneInfo("Asia/Shanghai")
INFO = {
    "name": "校赛",
    "reg_deadline": "2026-11-10 23:59",
    "contest_time": "2026-11-20 09:00",
    "mode": "线下",
    "location": "机房",
}


@pytest.fixture
def db_file(monkeypatch, tmp_path):
    path = tmp_path / "published.db"
    connections = []

    def connect():
        connection = sqlite3.connect(path)
        connections.append(connection)
        return connection

    monkeypatch.setattr(database, "_connect", connect)
    monkeypatch.setattr(database, "now_str", lambda: PUBLISHED_AT)
    monkeypatch.setattr(crawler, "FETCH_MIN_INTERVAL_MINUTES", 60)
    database.init_db()
    yield path, connections
    for connection in connections:
        connection.close()


@pytest.fixture
async def bot(monkeypatch, db_file, onebot_client):
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1", "g2"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "PUBLISH_CMD", "发布")
    monkeypatch.setattr(crawler, "AUTO_FETCH_ENABLED", False)
    state = SimpleNamespace(
        client=main.ACMBot(onebot_client), replies=[], sent=[], fetched=[], extracted=[]
    )

    async def reply(message, content):
        state.replies.append(content)
        return True

    async def send(group_id, content):
        state.sent.append((group_id, content))
        return True

    async def fetch(url):
        state.fetched.append(url)
        return PAGE

    async def extract(text):
        state.extracted.append(text)
        return dict(INFO), None

    monkeypatch.setattr(state.client, "_safe_reply", reply)
    monkeypatch.setattr(state.client, "_send_group", send)
    monkeypatch.setattr(main, "fetch_text", fetch)
    monkeypatch.setattr(main, "extract_contest", extract)
    return state


def _message(group="g1", member="teacher"):
    return OneBotMessage({"post_type": "message", "message_type": "group",
                         "raw_message": f"发布 {URL} 补充：校内截止提前一天",
                         "group_id": group, "user_id": member, "message_id": 12345,
                         "self_id": 30001})


def _now(hour=9, minute=0):
    return datetime(2026, 11, 1, hour, minute, tzinfo=TZ)


def _assert_no_records(path):
    with sqlite3.connect(path) as connection:
        for table in ("competitions", "competition_versions", "competition_sources"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


async def _publish(bot):
    await bot.client.on_group_at_message_create(_message())
    competitions = database.list_competitions(status="confirmed")
    assert len(competitions) == 1
    return competitions[0]


async def test_publish_registers_page_baseline_even_when_monitoring_disabled(bot):
    competition = await _publish(bot)
    assert bot.fetched == [URL]
    assert len(bot.extracted) == 1
    assert "补充：校内截止提前一天" in bot.extracted[0]
    sources = database.list_sources(competition["id"])
    assert len(sources) == 1
    source = sources[0]
    assert source["competition_id"] == competition["id"]
    assert source["url"] == competition["official_site"] == URL
    assert source["body_hash"] == hashlib.sha256(PAGE.encode("utf-8")).hexdigest()
    assert source["body_hash"] != hashlib.sha256(bot.extracted[0].encode("utf-8")).hexdigest()
    assert source["last_fetched_at"] == PUBLISHED_AT
    assert source["last_status"] == "confirmed"
    assert source["last_error"] == ""
    assert len(database.list_competition_versions(competition["id"])) == 1
    assert any("已免审发布" in reply for reply in bot.replies)
    assert {group for group, _ in bot.sent} == {"g1", "g2"}
    assert (await crawler.run_fetch(now=_now()))[0]["kind"] == "disabled"


@pytest.mark.parametrize("group,member", [("other", "teacher"), ("g1", "member")])
async def test_unauthorized_publish_does_not_fetch_or_register(bot, db_file, group, member):
    await bot.client.on_group_at_message_create(_message(group, member))
    assert bot.fetched == bot.extracted == bot.sent == []
    _assert_no_records(db_file[0])


@pytest.mark.parametrize("failure", ["fetch", "extract", "schema"])
async def test_failed_publish_leaves_no_source_or_competition(bot, db_file, monkeypatch, failure):
    async def fail_fetch(url):
        raise FetchError("模拟抓取失败")

    async def fail_extract(text):
        return None, "模拟提取失败"

    async def invalid_info(text):
        return {**INFO, "contest_time": "日期非法"}, None

    if failure == "fetch":
        monkeypatch.setattr(main, "fetch_text", fail_fetch)
    else:
        monkeypatch.setattr(main, "extract_contest", fail_extract if failure == "extract" else invalid_info)
    await bot.client.on_group_at_message_create(_message())
    _assert_no_records(db_file[0])
    assert bot.sent == []
    assert not any("已免审发布" in reply or "比赛信息已记录" in reply for reply in bot.replies)
    if failure == "fetch":
        assert bot.extracted == []


@pytest.mark.parametrize("operation", ["INSERT", "UPDATE"])
async def test_source_write_failure_rolls_back_publication(bot, db_file, operation):
    with sqlite3.connect(db_file[0]) as connection:
        connection.execute(
            f"CREATE TRIGGER fail_source BEFORE {operation} ON competition_sources "
            "BEGIN SELECT RAISE(ABORT, 'source write failure'); END"
        )
    await bot.client.on_group_at_message_create(_message())
    _assert_no_records(db_file[0])
    assert bot.sent == []
    assert bot.replies
    assert not any("已免审发布" in reply or "比赛信息已记录" in reply for reply in bot.replies)


@pytest.mark.parametrize(
    "info,url,text",
    [
        ({"name": ""}, URL, PAGE),
        ({"name": "校赛", "official_time": "日期非法"}, URL, PAGE),
        ({"name": "校赛"}, "", PAGE),
        ({"name": "校赛"}, None, PAGE),
        ({"name": "校赛"}, URL, 123),
    ],
)
def test_database_rejects_invalid_publication_without_partial_writes(db_file, info, url, text):
    with pytest.raises(ValueError):
        database.create_published_competition(info, url, text, operator="publish:teacher")
    _assert_no_records(db_file[0])


async def test_registered_source_obeys_interval_and_unchanged_page_avoids_model(bot, monkeypatch):
    competition = await _publish(bot)
    monkeypatch.setattr(crawler, "AUTO_FETCH_ENABLED", True)
    fetched = []
    extracted = []

    async def fetch(url):
        fetched.append(url)
        return PAGE, {"etag": '"new"', "last-modified": "Sun, 01 Nov 2026 08:00:00 GMT"}

    async def extract(text):
        extracted.append(text)
        raise AssertionError("正文未变化时不应调用模型")

    skipped = await crawler.run_fetch(now=_now(8, 5), fetch=fetch, extract=extract)
    assert skipped[0]["kind"] == "skip"
    assert fetched == extracted == []
    results = await crawler.run_fetch(now=_now(9), fetch=fetch, extract=extract)
    assert results[0]["kind"] == "nochange"
    assert results[0]["competition_id"] == competition["id"]
    assert fetched == [URL]
    assert extracted == []
    assert database.get_pending_update(competition["id"]) is None
    source = database.list_sources(competition["id"])[0]
    assert source["last_status"] == "nochange"
    assert source["etag"] == '"new"'


@pytest.mark.parametrize("model_url", [None, "", "https://wrong.example.com/invented"])
async def test_changed_page_creates_pending_update_and_preserves_source(bot, monkeypatch, model_url):
    original = await _publish(bot)
    publication_sends = list(bot.sent)
    monkeypatch.setattr(crawler, "AUTO_FETCH_ENABLED", True)
    changed_page = "校赛公告：报名截止改为 2026-11-15 23:59"
    fetched = []
    extracted = []

    async def fetch(url):
        fetched.append(url)
        return changed_page, {}

    async def extract(text):
        extracted.append(text)
        info = {**INFO, "reg_deadline": "2026-11-15 23:59"}
        if model_url is not None:
            info["source_link"] = model_url
        return info, None

    results = await crawler.run_fetch(now=_now(), fetch=fetch, extract=extract)
    assert len(results) == 1
    result = results[0]
    assert result["kind"] == "pending_auto"
    assert result["competition_id"] == original["id"]
    assert result["notify_groups"] == []
    assert fetched == [URL]
    assert extracted == [changed_page]
    assert database.get_competition(original["id"]) == original
    assert len(database.list_competitions()) == 1
    pending = database.get_pending_update(original["id"])
    source = database.list_sources(original["id"])[0]
    assert pending["source_id"] == source["id"]
    assert pending["fields"]["official_reg_deadline"] == "2026-11-15 23:59"
    assert pending["fields"]["official_site"] == URL
    assert source["body_hash"] == hashlib.sha256(changed_page.encode("utf-8")).hexdigest()
    assert bot.sent == publication_sends


async def test_published_source_survives_database_restart(bot, db_file, monkeypatch):
    competition = await _publish(bot)
    source_before = database.list_sources(competition["id"])[0]
    for connection in db_file[1]:
        connection.close()
    db_file[1].clear()
    # 重新初始化仍读取同一文件，所有后续调用使用新连接。
    database.init_db()
    assert database.list_sources(competition["id"]) == [source_before]
    monkeypatch.setattr(crawler, "AUTO_FETCH_ENABLED", True)
    fetched = []

    async def fetch(url):
        fetched.append(url)
        return PAGE, {}

    async def extract(text):
        raise AssertionError("重启不应丢失正文哈希而重新提取")

    results = await crawler.run_fetch(now=_now(), fetch=fetch, extract=extract)
    assert results[0]["kind"] == "nochange"
    assert fetched == [URL]
    assert len(database.list_competitions()) == 1
