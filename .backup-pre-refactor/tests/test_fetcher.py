"""抓取层 SSRF 防护与资源限制离线测试。

通过替换 getaddrinfo / _request_once 模拟网络行为，不发起真实请求。
"""

import asyncio
import ipaddress
import socket
import zlib

import pytest

import fetcher
from fetcher import FetchError, _is_public_ip, _parse_url


# ---------- URL 解析 ----------


def test_parse_url_ok():
    assert _parse_url("http://example.com/a") == ("http", "example.com", 80)
    assert _parse_url("https://example.com/a") == ("https", "example.com", 443)
    assert _parse_url("https://example.com:8443/a") == ("https", "example.com", 8443)


def test_parse_url_rejects_bad():
    assert _parse_url("ftp://example.com/a") is None
    assert _parse_url("file:///etc/passwd") is None
    assert _parse_url("javascript:alert(1)") is None
    assert _parse_url("http://user:pass@example.com/") is None  # 拒绝带凭据
    assert _parse_url("http:///path") is None


# ---------- 公网 IP 校验 ----------


def test_is_public_ip_rejects_private():
    for ip in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "172.16.0.1",
               "169.254.1.1", "224.0.0.1", "0.0.0.0", "255.255.255.255",
               "::1", "fe80::1", "fc00::1", "::"):
        assert _is_public_ip(ipaddress.ip_address(ip)) is False, ip


def test_is_public_ip_accepts_public():
    for ip in ("8.8.8.8", "114.114.114.114", "2606:4700:4700::1111"):
        assert _is_public_ip(ipaddress.ip_address(ip)) is True, ip


# ---------- 域名解析校验 ----------


class FakeLoop:
    def __init__(self, results):
        self.results = results

    async def getaddrinfo(self, host, port, type=None):
        return self.results


async def test_resolve_rejects_private_ip(monkeypatch):
    monkeypatch.setattr(fetcher.config, "FETCH_ALLOWED_DOMAINS", set())
    monkeypatch.setattr(
        asyncio, "get_running_loop",
        lambda: FakeLoop([(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]),
    )
    with pytest.raises(FetchError, match="非公网"):
        await fetcher._resolve_public_ip("evil.example.com")


async def test_resolve_rejects_mixed_private(monkeypatch):
    monkeypatch.setattr(fetcher.config, "FETCH_ALLOWED_DOMAINS", set())
    monkeypatch.setattr(
        asyncio, "get_running_loop",
        lambda: FakeLoop(
            [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
            ]
        ),
    )
    with pytest.raises(FetchError, match="非公网"):
        await fetcher._resolve_public_ip("evil.example.com")


async def test_resolve_returns_public(monkeypatch):
    monkeypatch.setattr(fetcher.config, "FETCH_ALLOWED_DOMAINS", set())
    monkeypatch.setattr(
        asyncio, "get_running_loop",
        lambda: FakeLoop([(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]),
    )
    assert await fetcher._resolve_public_ip("example.com") == "8.8.8.8"


async def test_resolve_domain_whitelist(monkeypatch):
    monkeypatch.setattr(fetcher.config, "FETCH_ALLOWED_DOMAINS", {"example.com"})
    with pytest.raises(FetchError, match="白名单"):
        await fetcher._resolve_public_ip("other.com")
    monkeypatch.setattr(
        asyncio, "get_running_loop",
        lambda: FakeLoop([(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]),
    )
    assert await fetcher._resolve_public_ip("example.com") == "8.8.8.8"


async def test_resolve_unresolvable(monkeypatch):
    monkeypatch.setattr(fetcher.config, "FETCH_ALLOWED_DOMAINS", set())

    class NoLoop:
        async def getaddrinfo(self, host, port, type=None):
            raise socket.gaierror("no such host")

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: NoLoop())
    with pytest.raises(FetchError, match="无法解析"):
        await fetcher._resolve_public_ip("no-such-host.invalid")


# ---------- 响应体限制 ----------


async def test_read_fixed_rejects_oversize(monkeypatch):
    monkeypatch.setattr(fetcher.config, "FETCH_MAX_BYTES", 100)

    class Reader:
        async def read(self, n):
            return b"x" * 200

    with pytest.raises(FetchError, match="大小上限"):
        await fetcher._read_fixed(Reader(), b"", 100, None)


async def test_read_chunked_rejects_bad_header(monkeypatch):
    monkeypatch.setattr(fetcher.config, "FETCH_MAX_BYTES", 10000)

    class Reader:
        async def read(self, n):
            return b"ZZZ\r\n"

    with pytest.raises(FetchError, match="chunk"):
        await fetcher._read_chunked(Reader(), b"", 10000)


async def test_read_chunked_oversize(monkeypatch):
    monkeypatch.setattr(fetcher.config, "FETCH_MAX_BYTES", 10)

    class Reader:
        async def read(self, n):
            return b""

    with pytest.raises(FetchError, match="大小上限|不完整"):
        await fetcher._read_chunked(Reader(), b"100\r\n" + b"x" * 256, 10)


def _gzip_bytes(data: bytes) -> bytes:
    """构造 gzip 格式压缩数据（与 _decompress_gzip 的 wbits=16+MAX_WBITS 匹配）。"""
    co = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    return co.compress(data) + co.flush()


def test_decompress_gzip_bomb(monkeypatch):
    monkeypatch.setattr(fetcher.config, "FETCH_MAX_BYTES", 100)
    # 100MB 全零压缩块，解压后会远超限制
    payload = _gzip_bytes(b"\x00" * (100 * 1024 * 1024))
    with pytest.raises(FetchError, match="解压后"):
        fetcher._decompress_gzip(payload)


def test_decompress_gzip_ok(monkeypatch):
    monkeypatch.setattr(fetcher.config, "FETCH_MAX_BYTES", 100000)
    data = _gzip_bytes(b"hello world" * 10)
    assert fetcher._decompress_gzip(data) == b"hello world" * 10


# ---------- 重定向跟随 ----------


async def test_fetch_text_follows_redirect(monkeypatch):
    monkeypatch.setattr(fetcher.config, "FETCH_MAX_REDIRECTS", 3)

    async def fake_request_once(url):
        if url == "http://a.com/start":
            return 302, {"location": "https://b.com/final"}, b""
        if url == "https://b.com/final":
            return 200, {"content-type": "text/plain"}, b"final content"
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(fetcher, "_request_once", fake_request_once)
    text = await fetcher.fetch_text("http://a.com/start")
    assert "final content" in text


async def test_fetch_text_redirect_too_many(monkeypatch):
    monkeypatch.setattr(fetcher.config, "FETCH_MAX_REDIRECTS", 2)

    async def fake_request_once(url):
        return 302, {"location": "http://a.com/next"}, b""

    monkeypatch.setattr(fetcher, "_request_once", fake_request_once)
    with pytest.raises(FetchError, match="重定向次数"):
        await fetcher.fetch_text("http://a.com/start")


async def test_fetch_text_redirect_no_location(monkeypatch):
    monkeypatch.setattr(fetcher.config, "FETCH_MAX_REDIRECTS", 2)

    async def fake_request_once(url):
        return 301, {}, b""

    monkeypatch.setattr(fetcher, "_request_once", fake_request_once)
    with pytest.raises(FetchError, match="Location"):
        await fetcher.fetch_text("http://a.com/start")


async def test_fetch_text_http_error(monkeypatch):
    monkeypatch.setattr(fetcher.config, "FETCH_MAX_REDIRECTS", 2)

    async def fake_request_once(url):
        return 500, {"content-type": "text/plain"}, b"error"

    monkeypatch.setattr(fetcher, "_request_once", fake_request_once)
    with pytest.raises(FetchError, match="500"):
        await fetcher.fetch_text("http://a.com/start")


async def test_fetch_text_unsupported_ctype(monkeypatch):
    monkeypatch.setattr(fetcher.config, "FETCH_MAX_REDIRECTS", 2)

    async def fake_request_once(url):
        return 200, {"content-type": "application/pdf"}, b"%PDF"

    monkeypatch.setattr(fetcher, "_request_once", fake_request_once)
    with pytest.raises(FetchError, match="不支持"):
        await fetcher.fetch_text("http://a.com/start")
