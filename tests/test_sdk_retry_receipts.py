"""真实 BotHttp/BotAPI 重试路径，受控 session 不发送外部请求。"""

import asyncio
import json
import sqlite3
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

import config
import database as db
import main
import reminder
from botpy.http import Route
from sdk_compat import ReceiptBotHttp


class Session:
    closed = True

    def __init__(self, outcomes, content_type="application/json"):
        self.outcomes = list(outcomes)
        self.requests = 0
        self.deliveries = 0
        self.content_type = content_type

    def request(self, **kwargs):
        self.requests += 1
        outcome = self.outcomes.pop(0)
        session = self

        class ResponseContext:
            async def __aenter__(self):
                if isinstance(outcome, Exception):
                    raise outcome
                session.deliveries += 1
                return SimpleNamespace(
                    status=200, headers={"content-type": session.content_type},
                    request_info=SimpleNamespace(url="https://example.invalid/mock"),
                    json=AsyncMock(return_value=outcome),
                    text=AsyncMock(return_value=json.dumps(outcome)),
                )

            async def __aexit__(self, *args):
                return False

        return ResponseContext()


def prepare(http, outcomes, content_type="application/json"):
    session = Session(outcomes, content_type)
    http._session = session
    http._headers = {}
    http.check_session = AsyncMock()
    return session


async def test_actual_sdk_reset_retry_returns_receipt():
    http = ReceiptBotHttp(timeout=1)
    session = prepare(http, [ConnectionResetError("reset"), {"id": "delivered"}])
    assert await http.request(Route("POST", "/mock"), json={"content": "test"}) == {"id": "delivered"}
    assert session.requests == 2
    assert session.deliveries == 1


async def test_exhausted_retry_and_timeout_keep_missing_receipt():
    http = ReceiptBotHttp(timeout=1)
    session = prepare(http, [ConnectionResetError("reset")] * 3)
    assert await http.request(Route("POST", "/mock")) is None
    assert session.requests == 3
    assert session.deliveries == 0
    prepare(http, [asyncio.TimeoutError()])
    assert await http.request(Route("POST", "/mock")) is None


async def test_success_of_other_request_cannot_supply_missing_receipt():
    http = ReceiptBotHttp(timeout=1)
    prepare(http, [{"id": "first"}, asyncio.TimeoutError()])
    assert await http.request(Route("POST", "/mock")) == {"id": "first"}
    assert await http.request(Route("POST", "/mock")) is None
    first = ReceiptBotHttp(timeout=1)
    second = ReceiptBotHttp(timeout=1)
    prepare(first, [ConnectionResetError("reset"), {"id": "concurrent"}])
    prepare(second, [asyncio.TimeoutError()])
    result = await asyncio.gather(first.request(Route("POST", "/mock")), second.request(Route("POST", "/mock")))
    assert result == [{"id": "concurrent"}, None]


@pytest.mark.parametrize("content_type", ["application/json", "application/json; charset=utf-8", "Application/JSON; Charset=UTF-8"])
async def test_reset_success_persists_and_next_scan_does_not_repeat(monkeypatch, content_type):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(db, "_connect", lambda: conn)
    db.init_db()
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1"})
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    cid = db.create_collection("g1", "SDK receipt", deadline="2026-11-01 09:00")
    client = main.ACMBot()
    session = prepare(client.http, [ConnectionResetError("reset"), {"id": "accepted"}], content_type)
    now = datetime(2026, 11, 1, 8, tzinfo=reminder.TZ)
    try:
        assert await reminder.check_general_reminders(client._send_group, now=now) == 1
        assert await reminder.check_general_reminders(client._send_group, now=now) == 0
        records = conn.execute("SELECT obj_id, status, sent_at FROM reminder_deliveries WHERE group_id = 'g1'").fetchall()
        assert len(records) == 1 and records[0][0] == cid
        assert records[0][1] == "sent" and records[0][2]
        assert session.requests == 2 and session.deliveries == 1
    finally:
        conn.close()


@pytest.mark.parametrize("outcome", [None, {}, {"id": ""}, {"id": 123}, {"id": "receipt", "code": 1}, "not json"])
async def test_textual_invalid_or_failed_receipt_is_not_success(monkeypatch, outcome):
    monkeypatch.setattr(config, "ALLOWED_GROUPS", {"g1"})
    monkeypatch.setattr(config, "GROUP_ACTIVE_MESSAGES_ENABLED", True)
    client = main.ACMBot()
    prepare(client.http, [outcome], "application/json; charset=utf-8")
    assert await client._send_group("g1", "test") is False
