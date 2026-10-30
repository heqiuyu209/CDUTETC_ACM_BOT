"""真实流读取和 gzip 解压的离线回归，不建立网络连接。"""

import asyncio
import gzip
import ipaddress
import tracemalloc

import pytest

import fetcher
from fetcher import FetchError


class MemoryReader:
    def __init__(self, data, block_size=None):
        self.data = data
        self.block_size = block_size

    async def read(self, size):
        if self.block_size is not None:
            size = min(size, self.block_size)
        block, self.data = self.data[:size], self.data[size:]
        return block


class MemoryWriter:
    def __init__(self):
        self.request = b""
        self.closed = False

    def write(self, data):
        self.request += data

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


@pytest.fixture
def connection(monkeypatch):
    state = {"response": b"", "block_size": None, "writers": [], "hosts": []}

    async def resolve(host):
        state["hosts"].append(host)
        return "8.8.8.8"

    async def connect(host, ip, port, scheme):
        writer = MemoryWriter()
        state["writers"].append(writer)
        return MemoryReader(state["response"], state["block_size"]), writer

    monkeypatch.setattr(fetcher, "_resolve_public_ip", resolve)
    monkeypatch.setattr(fetcher, "_connect", connect)
    monkeypatch.setattr(fetcher.config, "FETCH_MAX_BYTES", 1024 * 1024)
    return state


def response(body, headers=b""):
    return b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n" + headers + b"\r\n" + body


@pytest.mark.parametrize("body,headers", [
    (b"short", b"Content-Length: 100\r\n"),
    (b"longer", b"Content-Length: 3\r\n"),
    (gzip.compress(b"complete text")[:-8], b"Content-Encoding: gzip\r\n"),
    (gzip.compress(b"complete text")[:-1], b"Content-Encoding: gzip\r\n"),
    (b"not gzip", b"Content-Encoding: gzip\r\n"),
    (b"1\r\nx\r\n0\r\n", b"Transfer-Encoding: chunked\r\n"),
    (b"3\r\nxy", b"Transfer-Encoding: chunked\r\n"),
    (b"1\r\nxXX0\r\n\r\n", b"Transfer-Encoding: chunked\r\n"),
    (b"1\r\nx\r\n0\r\nContent-Length: 1\r\n\r\n", b"Transfer-Encoding: chunked\r\n"),
    (b"x", b"Content-Length: 1\r\nContent-Length: 2\r\n"),
    (b"x", b"Transfer-Encoding: chunked\r\nContent-Length: 1\r\n"),
    (b"x", b"Transfer-Encoding: unknown-chunked\r\n"),
])
async def test_incomplete_or_ambiguous_responses_never_succeed(connection, body, headers):
    connection["response"] = response(body, headers)
    with pytest.raises(FetchError):
        await fetcher.fetch_text("https://contest.example.com/news")
    assert connection["writers"][0].closed


@pytest.mark.parametrize("body,headers,expected", [
    (b"hello", b"Content-Length: 5\r\n", "hello"),
    (b"hello", b"", "hello"),
    (gzip.compress(b"hello"), b"Content-Encoding: gzip\r\n", "hello"),
    (gzip.compress(b"hello") + gzip.compress(b" world"), b"Content-Encoding: gzip\r\n", "hello world"),
    (b"2;name=value\r\nhe\r\n3\r\nllo\r\n0\r\nX-Checksum: ok\r\n\r\n", b"Transfer-Encoding: chunked\r\n", "hello"),
    (b"", b"Content-Length: 0\r\n", ""),
])
async def test_complete_responses_survive_fragmented_reads(connection, body, headers, expected):
    connection["response"] = response(body, headers)
    connection["block_size"] = 3
    assert await fetcher.fetch_text("https://contest.example.com/news") == expected
    assert connection["writers"][0].closed


async def test_informational_response_is_not_treated_as_page(connection):
    connection["response"] = b"HTTP/1.1 103 Early Hints\r\nLink: </style.css>\r\n\r\n" + response(b"hello", b"Content-Length: 5\r\n")
    assert await fetcher.fetch_text("https://contest.example.com/news") == "hello"


@pytest.mark.parametrize("initial,limit,expected", [
    (b"abcd", 3, None),
    (b"abcd", 10, 3),
    (b"x", 0, None),
])
async def test_initial_body_buffer_obeys_limits(initial, limit, expected):
    with pytest.raises(FetchError):
        await fetcher._read_fixed(MemoryReader(b""), initial, limit, expected)


def test_gzip_output_budget_limits_real_peak_memory(monkeypatch):
    limit = 1024 * 1024
    payload = gzip.compress(b"x" * (8 * limit))
    monkeypatch.setattr(fetcher.config, "FETCH_MAX_BYTES", limit)
    tracemalloc.start()
    try:
        with pytest.raises(FetchError, match="解压后"):
            fetcher._decompress_gzip(payload)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # 旧代码会先展开 8 MiB，峰值超过 20 MiB；当前限制实际峰值。
    assert peak < 3 * limit


def test_gzip_crc_failure_is_rejected(monkeypatch):
    payload = bytearray(gzip.compress(b"hello world"))
    payload[-8] ^= 1
    with pytest.raises(FetchError, match="gzip"):
        fetcher._decompress_gzip(bytes(payload))


def test_gzip_members_share_budget_and_exact_limit_is_valid(monkeypatch):
    monkeypatch.setattr(fetcher.config, "FETCH_MAX_BYTES", 10)
    assert fetcher._decompress_gzip(gzip.compress(b"x" * 10)) == b"x" * 10
    with pytest.raises(FetchError, match="解压后"):
        fetcher._decompress_gzip(gzip.compress(b"x" * 6) + gzip.compress(b"y" * 5))
    with pytest.raises(FetchError, match="gzip"):
        fetcher._decompress_gzip(gzip.compress(b"hello") + b"trailing garbage")


@pytest.mark.parametrize("ip", ["100.64.0.1", "100.127.255.254", "::ffff:100.64.0.1", "::ffff:127.0.0.1", "224.0.0.1", "ff02::1"])
def test_only_globally_routable_unicast_addresses_are_allowed(ip):
    assert not fetcher._is_public_ip(ipaddress.ip_address(ip))


async def test_chinese_url_idna_and_port_are_encoded_without_losing_data(connection):
    connection["response"] = response(b"ok", b"Content-Length: 2\r\n")
    url = "https://例子.测试:8443/比赛%20名?届次=2026&a=1,b=2#片段"
    assert await fetcher.fetch_text(url) == "ok"
    assert connection["hosts"] == ["xn--fsqu00a.xn--0zwm56d"]
    request = connection["writers"][0].request.decode("ascii")
    assert request.startswith("GET /%E6%AF%94%E8%B5%9B%20%E5%90%8D?%E5%B1%8A%E6%AC%A1=2026&a=1,b=2 HTTP/1.1\r\n")
    assert "\r\nHost: xn--fsqu00a.xn--0zwm56d:8443\r\n" in request
    assert "#" not in request.split("\r\n", 1)[0]


async def test_ipv6_host_is_bracketed_and_empty_path_is_slash(connection):
    connection["response"] = response(b"ok", b"Content-Length: 2\r\n")
    assert await fetcher.fetch_text("http://[2606:4700:4700::1111]:8080?x=1") == "ok"
    request = connection["writers"][0].request.decode("ascii")
    assert request.startswith("GET /?x=1 HTTP/1.1\r\n")
    assert "\r\nHost: [2606:4700:4700::1111]:8080\r\n" in request


async def test_request_preserves_semicolon_path_parameters(connection):
    connection["response"] = response(b"ok", b"Content-Length: 2\r\n")
    assert await fetcher.fetch_text("https://contest.example.com/news;edition=2026?q=a;b") == "ok"
    assert connection["writers"][0].request.startswith(b"GET /news;edition=2026?q=a;b HTTP/1.1\r\n")


async def test_header_budget_does_not_include_buffered_body(monkeypatch):
    monkeypatch.setattr(fetcher, "_MAX_HEADER_BYTES", 128)
    prefix = b"HTTP/1.1 200 OK\r\nX-Padding: "
    header = prefix + b"x" * (128 - len(prefix) - 4) + b"\r\n\r\n"
    status, headers, body = await fetcher._read_headers(MemoryReader(header + b"body"))
    assert status == 200
    assert body == b"body"
    assert headers["x-padding"]


async def test_excessively_long_content_length_is_fetch_error(connection):
    connection["response"] = response(b"x", b"Content-Length: " + b"9" * 5000 + b"\r\n")
    with pytest.raises(FetchError, match="Content-Length"):
        await fetcher.fetch_text("https://contest.example.com/news")
    assert connection["writers"][0].closed


@pytest.mark.parametrize("url", ["https://example.com:bad/", "https://example.com:70000/", "https://example.com:0/", "https://@example.com/", "https://example.com/\r\nInjected: yes"])
async def test_invalid_url_is_fetch_error_before_connecting(connection, url):
    with pytest.raises(FetchError):
        await fetcher.fetch_text(url)
    assert connection["hosts"] == []
    assert connection["writers"] == []


async def test_request_timeout_covers_dns_and_reading(monkeypatch):
    monkeypatch.setattr(fetcher.config, "FETCH_TIMEOUT", 0.02)

    async def unresolved(host):
        await asyncio.Event().wait()

    monkeypatch.setattr(fetcher, "_resolve_public_ip", unresolved)
    with pytest.raises(FetchError, match="超时"):
        await fetcher.fetch_text("https://contest.example.com/news")
