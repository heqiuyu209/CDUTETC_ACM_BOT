"""OneBot 事件入口的发布边界与持久化回归。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import sqlite3

import pytest
from onebot_client import OneBotMessage

import commands
import config
import database as db
import main
from utils import is_publish_command


@pytest.mark.parametrize("text", [
    "发布的这个校赛为什么报名时间不同？ https://contest.example.com",
    "发布会什么时候开始？ https://contest.example.com",
    "发布失败怎么办？ https://contest.example.com",
])
async def test_question_does_not_publish_or_fetch(monkeypatch, text):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(db, "_connect", lambda: conn)
    db.init_db()
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "PUBLISH_CMD", "发布")
    fetch = AsyncMock()
    extract = AsyncMock()
    monkeypatch.setattr(main, "fetch_text", fetch)
    monkeypatch.setattr(main, "extract_contest", extract)
    monkeypatch.setattr(commands, "handle_non_command_message", AsyncMock(return_value=[]))
    client = main.ACMBot(SimpleNamespace(send_group_msg=AsyncMock(return_value=True)))
    message = OneBotMessage({
        "message_id": 1, "raw_message": text, "group_id": "g1",
        "message_type": "group", "user_id": "teacher",
    })
    try:
        await client.on_group_at_message_create(message)
        fetch.assert_not_awaited()
        extract.assert_not_awaited()
        assert db.list_competitions() == []
        assert db.list_sources() == []
    finally:
        conn.close()


@pytest.mark.parametrize("text", ["发布", "发布 https://contest.example.com", "发布\nhttps://contest.example.com", "发布\u3000https://contest.example.com", "发布https://contest.example.com", "发布http://contest.example.com"])
def test_explicit_publish_command_keeps_supported_whitespace(text):
    assert is_publish_command(text)


def test_custom_command_requires_its_own_boundary():
    assert is_publish_command("/发布 https://contest.example.com", "/发布")
    assert not is_publish_command("/发布的比赛？", "/发布")
    assert not is_publish_command("普通问题", "")


@pytest.mark.parametrize("separator", ["", " ", "\n", "\u3000"])
async def test_onebot_explicit_url_publication_keeps_documented_syntax(monkeypatch, separator):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(db, "_connect", lambda: conn)
    db.init_db()
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1"})
    monkeypatch.setattr(config, "TEACHER_WHITELIST", {"teacher"})
    monkeypatch.setattr(config, "PUBLISH_CMD", "发布")
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", False)
    fetch = AsyncMock(return_value="校赛公告")
    extract = AsyncMock(return_value=({"name": "校赛"}, None))
    monkeypatch.setattr(main, "fetch_text", fetch)
    monkeypatch.setattr(main, "extract_contest", extract)
    client = main.ACMBot(SimpleNamespace(send_group_msg=AsyncMock(return_value=True)))
    message = OneBotMessage({
        "message_id": 1, "raw_message": f"发布{separator}https://contest.example.com", "group_id": "g1",
        "message_type": "group", "user_id": "teacher",
    })
    try:
        await client.on_group_at_message_create(message)
        fetch.assert_awaited_once_with("https://contest.example.com")
        extract.assert_awaited_once()
        assert len(db.list_competitions(status="confirmed")) == 1
        assert len(db.list_sources()) == 1
    finally:
        conn.close()
