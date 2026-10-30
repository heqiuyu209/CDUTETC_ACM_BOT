"""完整正文、字符集、增量解析和 DNS 回退；所有网络与模型调用均为离线替身。"""

import asyncio
import codecs
from datetime import datetime
import socket
import ssl
import time
import tracemalloc
from zoneinfo import ZoneInfo

import pytest

import crawler
import database
import fetcher
import llm
from fetcher import FetchError


URL = "https://contest.example.com/news"


@pytest.fixture
def page(monkeypatch):
    state = {"body": b"", "type": "text/html; charset=utf-8", "etag": '"old"'}

    async def request(url):
        return 200, {"content-type": state["type"], "etag": state["etag"]}, state["body"]

    monkeypatch.setattr(fetcher, "_request_once", request)
    monkeypatch.setattr(fetcher.config, "FETCH_MAX_BYTES", 1_000_000)
    return state


async def test_tail_change_creates_pending_update_and_hashes_complete_text(page, monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "source.db"))
    monkeypatch.setattr(crawler, "AUTO_FETCH_ENABLED", True)
    monkeypatch.setattr(fetcher.config, "FETCH_TEXT_LIMIT", 3000)
    database.init_db()
    prefix = "<p>比赛公告</p><p>" + "A" * 3500 + "</p><p>报名截止："
    page["body"] = (prefix + "2026-11-10 23:59</p>").encode()
    old_text = await fetcher.fetch_text(URL)
    cid = database.create_published_competition(
        {"name": "离线校赛", "official_reg_deadline": "2026-11-10 23:59"}, URL, old_text,
    )
    source = database.list_sources(cid)[0]
    assert source["body_hash"] == crawler._body_hash(old_text)
    page["body"] = (prefix + "2026-11-20 23:59</p>").encode()
    page["etag"] = '"new"'
    new_text = await fetcher.fetch_text(URL)
    assert len(new_text) > 3500 and new_text.endswith("2026-11-20 23:59")
    assert old_text != new_text
    calls = []

    async def extract(text):
        calls.append(text)
        return {"name": "离线校赛", "reg_deadline": "2026-11-20 23:59"}, None

    result = await crawler.run_fetch(
        now=datetime(2026, 11, 1, 10, tzinfo=ZoneInfo("Asia/Shanghai")), extract=extract,
    )
    assert result[0]["kind"] == "pending_auto"
    assert calls == [new_text]
    assert database.list_sources(cid)[0]["body_hash"] == crawler._body_hash(new_text)
    assert database.list_pending_updates()[0]["fields"]["official_reg_deadline"] == "2026-11-20 23:59"
    assert database.get_competition(cid)["official_reg_deadline"] == "2026-11-10 23:59"


async def test_model_rejects_uncovered_body_instead_of_silently_truncating(page, monkeypatch):
    page.update(body=("<p>" + "比赛公告" * 2000 + "</p>").encode())
    monkeypatch.setattr(llm.config, "LLM_MAX_INPUT_CHARS", 120)
    calls = []

    async def chat(system, user):
        calls.append(user)
        return '{"name":"离线校赛"}'

    monkeypatch.setattr(llm, "_chat_once", chat)
    text = await fetcher.fetch_text(URL)
    assert len(text) == 8000
    info, error = await llm.extract_contest(text)
    assert info is None and "超过完整提取预算" in error
    assert calls == []


@pytest.mark.parametrize("body,ctype,expected", [
    ("<p>全国程序设计竞赛🧑</p>".encode("gb18030"), "text/html; CHARSET=\"gb18030\"", "全国程序设计竞赛🧑"),
    ("<meta charset='gb18030'><p>中文竞赛</p>".encode("gb18030"), "text/html", "中文竞赛"),
    ('<meta content="text/html; charset=gb18030" http-equiv="Content-Type"><p>中文竞赛</p>'.encode("gb18030"), "text/html", "中文竞赛"),
    ("中文正文".encode("gb18030"), "text/plain; charset=gb18030", "中文正文"),
    (codecs.BOM_UTF8 + "中文正文".encode(), "text/plain; charset=gb18030", "中文正文"),
    ("<p>中文正文</p>".encode("utf-16"), "text/html", "中文正文"),
    ("中文正文".encode("utf-32"), "text/plain", "中文正文"),
    ("<p>普通UTF-8公告</p>".encode(), "text/html", "普通UTF-8公告"),
    ('<meta charset="gb18030"><p>UTF-8公告</p>'.encode(), "text/html; charset=utf-8", "UTF-8公告"),
    (b"caf\xe9", "text/plain; charset=iso-8859-1", "café"),
])
async def test_declared_charsets_and_boms(page, body, ctype, expected):
    page.update(body=body, type=ctype)
    assert await fetcher.fetch_text(URL) == expected


@pytest.mark.parametrize("charset", ["made-up-charset", "base64_codec", "rot_13", "unicode_escape"])
async def test_unknown_or_transform_charset_is_fetch_error(page, charset):
    page.update(body=b"YQ==", type=f"text/plain; charset={charset}")
    with pytest.raises(FetchError, match="字符集"):
        await fetcher.fetch_text(URL)


@pytest.mark.parametrize("size", [1, 2, 7, 2048])
async def test_text_and_entities_do_not_depend_on_parser_chunk_boundaries(page, monkeypatch, size):
    monkeypatch.setattr(fetcher, "_PARSE_BLOCK_CHARS", size)
    page["body"] = (
        "<p>长公告A B&amp;C&#x4E2D; D</p><!-- ignored -->"
        "<script>bad < text</script><style>bad</style>"
        "<noscript><p>bad</p></noscript><iframe>bad</iframe><p>截止 2026-11-20</p>"
    ).encode()
    assert await fetcher.fetch_text(URL) == "长公告A B&C中 D\n截止 2026-11-20"


@pytest.mark.parametrize("tag", ["script", "style"])
async def test_long_ignored_cdata_is_discarded_with_split_closing_tag(page, monkeypatch, tag):
    monkeypatch.setattr(fetcher, "_PARSE_BLOCK_CHARS", 17)
    page["body"] = f"<p>before</p><{tag}>".encode() + b"x" * 800_000 + f"</{tag}><p>after</p>".encode()
    assert await fetcher.fetch_text(URL) == "before\nafter"


@pytest.mark.parametrize("suffix", [b'<div title="' + b"x" * 20_000, b"<!--" + b"x" * 20_000])
async def test_unfinished_html_tokens_are_bounded(page, suffix):
    page["body"] = b"<p>visible</p>" + suffix
    with pytest.raises(FetchError, match="HTML.*大小上限"):
        await fetcher.fetch_text(URL)


async def test_hidden_nesting_is_bounded(page):
    page["body"] = b"<noscript>" * 129
    with pytest.raises(FetchError, match="嵌套"):
        await fetcher.fetch_text(URL)


def test_dense_tags_have_bounded_real_peak_memory():
    body = b"<p>offline contest</p>" + b"<b/>" * 249_000
    tracemalloc.start()
    try:
        assert fetcher._extract_text(body, "text/html", URL) == "offline contest"
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # 旧 DOM 峰值约 139 MB；增量解析无需为 249000 个标签保留对象。
    assert peak < 4 * len(body)


async def test_dense_html_yields_to_other_tasks(page):
    page["body"] = b"<p>offline contest</p>" + b"<b/>" * 249_000
    ticks = []

    async def ticker():
        while True:
            ticks.append(1)
            await asyncio.sleep(0)

    task = asyncio.create_task(ticker())
    try:
        assert await fetcher.fetch_text(URL) == "offline contest"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(ticks) > 100


async def test_total_timeout_includes_parsing(page, monkeypatch):
    page["body"] = b"<p>offline contest</p>" + b"<b/>" * 249_000
    monkeypatch.setattr(fetcher.config, "FETCH_TIMEOUT", 0.01)
    started = time.monotonic()
    with pytest.raises(FetchError, match="超时"):
        await fetcher.fetch_text(URL)
    assert time.monotonic() - started < 0.25


class Reader:
    def __init__(self, data):
        self.data = data
        self.offset = 0

    async def read(self, size):
        data = self.data[self.offset:self.offset + size]
        self.offset += len(data)
        return data


class Writer:
    def __init__(self):
        self.closed = False

    def write(self, data):
        pass

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


def dns_results(addresses):
    return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in addresses]


async def test_dns_fallback_pins_ips_and_preserves_https_verification(monkeypatch):
    first, second = "2001:4860:4860::8888", "8.8.8.8"
    attempts = []
    writer = Writer()

    async def resolve(host, port, **kwargs):
        return dns_results([first, first, second])

    async def connect(**kwargs):
        attempts.append(kwargs["host"])
        assert kwargs["server_hostname"] == "contest.example.com"
        assert kwargs["ssl"].verify_mode == ssl.CERT_REQUIRED
        assert kwargs["ssl"].check_hostname
        if kwargs["host"] == first:
            raise OSError("offline IPv6 unreachable")
        return Reader(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nok"), writer

    monkeypatch.setattr(fetcher.config, "FETCH_ALLOWED_DOMAINS", set())
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(fetcher.asyncio, "open_connection", connect)
    assert await fetcher.fetch_text(URL) == "ok"
    assert attempts == [first, second]
    assert writer.closed


async def test_mixed_dns_results_are_all_checked_before_any_connection(monkeypatch):
    async def resolve(host, port, **kwargs):
        return dns_results(["8.8.8.8", "127.0.0.1"])

    async def connect(*args, **kwargs):
        pytest.fail("connection occurred before all DNS results were validated")

    monkeypatch.setattr(fetcher.config, "FETCH_ALLOWED_DOMAINS", set())
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(fetcher, "_connect", connect)
    with pytest.raises(FetchError, match="非公网"):
        await fetcher.fetch_text(URL)


async def test_unresponsive_first_address_leaves_budget_for_second(monkeypatch):
    attempts = []
    first, second = "2001:4860:4860::8888", "8.8.8.8"

    async def resolve(host):
        await asyncio.sleep(0.01)
        return [first, second]

    async def connect(host, ip, port, scheme):
        attempts.append(ip)
        if ip == first:
            await asyncio.Event().wait()
        return Reader(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nok"), Writer()

    monkeypatch.setattr(fetcher.config, "FETCH_TIMEOUT", 0.12)
    monkeypatch.setattr(fetcher, "_resolve_public_ips", resolve)
    monkeypatch.setattr(fetcher, "_connect", connect)
    started = time.monotonic()
    assert await fetcher.fetch_text(URL) == "ok"
    assert attempts == [first, second]
    assert time.monotonic() - started < 0.2


async def test_dns_connect_read_and_writer_cleanup_share_one_timeout(monkeypatch):
    calls = []
    writer = Writer()

    async def resolve(host):
        await asyncio.sleep(0.02)
        return ["8.8.8.8"]

    class SlowReader(Reader):
        async def read(self, size):
            calls.append("read")
            await asyncio.Event().wait()

    async def connect(host, ip, port, scheme):
        await asyncio.sleep(0.02)
        return SlowReader(b""), writer

    async def wait_closed():
        await asyncio.Event().wait()

    writer.wait_closed = wait_closed
    monkeypatch.setattr(fetcher.config, "FETCH_TIMEOUT", 0.06)
    monkeypatch.setattr(fetcher, "_resolve_public_ips", resolve)
    monkeypatch.setattr(fetcher, "_connect", connect)
    started = time.monotonic()
    with pytest.raises(FetchError, match="超时"):
        await fetcher.fetch_text(URL)
    assert calls and writer.closed
    assert time.monotonic() - started < 0.15
