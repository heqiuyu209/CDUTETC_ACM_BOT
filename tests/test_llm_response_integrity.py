"""受控 HTTPX 流验证实际响应字节预算与问答入口回执。"""

import gzip
import json
import sqlite3
from unittest.mock import AsyncMock

import httpx
import pytest
import commands
import config
import database as db
import llm


def transport(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))


@pytest.mark.parametrize("content,expected", [
    ("报名截止是 11 月 10 日。", "报名截止是 11 月 10 日。"),
    ('{"answer":"报名截止是 11 月 10 日。"}', "报名截止是 11 月 10 日。"),
    ('```json\n{"answer":"报名截止是 11 月 10 日。"}\n```', "报名截止是 11 月 10 日。"),
    ('```JSON\r\n{"answer":"报名截止是 11 月 10 日。"}\r\n```', "报名截止是 11 月 10 日。"),
    ('```\n{"answer":"报名截止是 11 月 10 日。"}\n```', "报名截止是 11 月 10 日。"),
    ('```json {"answer":"报名截止是 11 月 10 日。"}```', "报名截止是 11 月 10 日。"),
    ('~~~json\n{"answer":"报名截止是 11 月 10 日。"}\n~~~', "报名截止是 11 月 10 日。"),
    ('````json\n{"answer":"报名截止是 11 月 10 日。"}\n`````', "报名截止是 11 月 10 日。"),
    ('"报名截止是 11 月 10 日。"', "报名截止是 11 月 10 日。"),
    ('```text\n报名截止是 11 月 10 日。\n```', '```text\n报名截止是 11 月 10 日。\n```'),
    ('```json\n报名截止是 11 月 10 日。\n```', '```json\n报名截止是 11 月 10 日。\n```'),
    ('```python\nfor i in range(3):\n    print(i)\n```', '```python\nfor i in range(3):\n    print(i)\n```'),
    ('```text\n{这是普通说明，不是 JSON}\n```', '```text\n{这是普通说明，不是 JSON}\n```'),
    ("2026", "2026"),
    ("50", "50"),
    ('```text\n50\n```', '```text\n50\n```'),
])
async def test_qa_uses_text_mode_and_unwraps_valid_answer(monkeypatch, content, expected):
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert "response_format" not in body
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
    transport(monkeypatch, handler)
    assert await llm.answer_question("校赛怎么报名？", []) == expected
    assert len(requests) == 1


@pytest.mark.parametrize("content", [
    "", "  ", None, '{"answer":""}', '{"unexpected":"value"}',
    "{}", "[]", "null", '""', '[{"answer":"不应把数组当成回答"}]',
    '```json\n{}\n```', '```json\n{"answer":""}\n```',
    '```json\n{"answer":"  "}\n```', '```json\n{"answer":null}\n```',
    '```json\n{"answer":123}\n```', '```json\n{"unexpected":"value"}\n```',
    '```json\n[]\n```', '```json\nnull\n```', '```json\n""\n```',
    '```\n[]\n```', '~~~json\nnull\n~~~', '```json {} ```',
    '```json\n\n```', '```text\n\n```', '```\n\n```',
    '```json\n{}\n````', '`' * 8192,
])
async def test_invalid_answer_is_recorded_and_replied(monkeypatch, content):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(db, "_connect", lambda: conn)
    db.init_db()
    transport(monkeypatch, lambda request: httpx.Response(200, json={"choices": [{"message": {"content": content}}]}))
    try:
        replies = await commands.handle_non_command_message("校赛怎么报名？", "g1", "student")
        assert len(db.list_unanswered("g1")) == 1
        assert len(replies) == 1 and "已记录为问题" in replies[0]["text"]
    finally:
        conn.close()


async def test_empty_external_qa_adapter_is_also_recorded(monkeypatch):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(db, "_connect", lambda: conn)
    db.init_db()
    monkeypatch.setattr(llm, "answer_question", AsyncMock(return_value=""))
    try:
        replies = await commands.handle_non_command_message("下次校赛呢？", "g1", "student")
        assert replies[0]["text"] and len(db.list_unanswered("g1")) == 1
    finally:
        conn.close()


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.read_bytes = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.read_bytes += len(chunk)
            yield chunk

    async def aclose(self):
        self.closed = True


async def test_actual_stream_stops_oversized_response_even_with_low_max_tokens(monkeypatch):
    monkeypatch.setattr(config, "LLM_MAX_RESPONSE_BYTES", 32768)
    monkeypatch.setattr(config, "LLM_MAX_OUTPUT_TOKENS", 16)
    stream = Stream([b"X" * 8192] * 1024)
    transport(monkeypatch, lambda req: httpx.Response(200, stream=stream))
    with pytest.raises(ValueError, match="字节上限"):
        await llm._chat_once("Return JSON", "test")
    assert stream.read_bytes <= 40960 and stream.closed


async def test_declared_large_response_rejected_before_body_read(monkeypatch):
    monkeypatch.setattr(config, "LLM_MAX_RESPONSE_BYTES", 64)
    stream = Stream([b"never consumed"])
    transport(monkeypatch, lambda req: httpx.Response(200, headers={"content-length": "1000000"}, stream=stream))
    with pytest.raises(ValueError, match="字节上限"):
        await llm._chat_once("Return JSON", "test")
    assert stream.read_bytes == 0 and stream.closed


async def test_gzip_budget_is_applied_to_decompressed_body(monkeypatch):
    monkeypatch.setattr(config, "LLM_MAX_RESPONSE_BYTES", 32768)
    stream = Stream([gzip.compress(b"X" * 8388608)])
    transport(monkeypatch, lambda req: httpx.Response(200, headers={"content-encoding": "gzip"}, stream=stream))
    with pytest.raises(ValueError, match="字节上限"):
        await llm._chat_once("Return JSON", "test")
    assert stream.closed


async def test_small_stream_json_and_extraction_keep_json_mode(monkeypatch):
    body = json.dumps({"choices": [{"message": {"content": '{"name":"校赛"}'}}]}).encode()
    stream = Stream([body[:15], body[15:]])
    def handler(request):
        payload = json.loads(request.content)
        assert payload["response_format"] == {"type": "json_object"}
        assert "JSON" in payload["messages"][0]["content"]
        return httpx.Response(200, stream=stream)
    transport(monkeypatch, handler)
    info, error = await llm.extract_contest("校赛公告")
    assert error is None and info["name"] == "校赛" and stream.closed
