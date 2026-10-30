"""链接抓取模块：带 SSRF 防护与资源限制的正文抓取。

设计要点：
- 仅允许 HTTP/HTTPS，拒绝带用户名/密码的 URL。
- 每次连接前解析并校验目标主机全部地址：回环、私网、链路本地、保留地址、
  组播、未指定地址一律拒绝（IPv4 + IPv6）。
- 用 socket 直连「已校验的 IP」发起请求，而不是让请求库独立二次解析，
  从机制上规避 DNS 重绑定 / TOCTOU（检查时解析结果 ≠ 实际连接地址）。
  HTTPS 时 SNI 与证书校验仍使用原始域名。
- 手动跟随重定向，每跳重新解析与校验；跳数上限由配置控制。
- 流式读取，限制读取与解压后响应体大小，超限立即中止；
  Content-Length 仅用于提前拒绝，不作为唯一保护。
- 可选域名白名单（推荐配置），抓取目标与跳转目标统一校验。
- 失败抛 FetchError（含原因），由上层决定如何提示，绝不返回可入库的空正文。
"""

import asyncio
import ipaddress
import logging
import re
import socket
import ssl
import zlib
from urllib.parse import quote, urljoin, urlparse, urlsplit

from bs4 import BeautifulSoup

import config

logger = logging.getLogger(__name__)

_ALLOWED_SCHEMES = ("http", "https")
_ALLOWED_CTYPES = {
    "text/html", "text/plain", "text/markdown",
    "application/xhtml+xml", "application/xml", "text/xml", "application/json",
}
_UA = "Mozilla/5.0 (compatible; CDUTETC-ACM-Bot/1.0; +https://q.qq.com)"
_MAX_HEADER_BYTES = 65536
_HEADER_NAME = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+")

_sem = asyncio.Semaphore(config.FETCH_CONCURRENCY)


class FetchError(Exception):
    """抓取失败（含安全拒绝），携带用户可读原因。"""


# ---------- 安全校验 ----------


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """非公网地址一律拒绝：回环/私网/链路本地/保留/组播/未指定。"""
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        return _is_public_ip(mapped)
    return ip.is_global and not (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _parse_url(url: str) -> tuple[str, str, int] | None:
    """解析 URL，仅接受无凭据的 http/https，返回 (scheme, host, port)。"""
    if not isinstance(url, str) or any(ord(c) < 32 or ord(c) == 127 for c in url):
        return None
    try:
        parsed = urlparse(url)
        if parsed.scheme not in _ALLOWED_SCHEMES or parsed.username is not None or parsed.password is not None:
            return None
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        if not host or "%" in host or not 1 <= port <= 65535:
            return None
    except (ValueError, UnicodeError):
        return None
    return parsed.scheme, host, port


async def _resolve_public_ip(host: str) -> str:
    """解析主机名并校验所有地址均为公网；返回第一个可用 IP。

    域名白名单（若配置）在此检查；白名单校验在解析之前，避免先做 DNS。
    """
    allowed = {d.encode("idna").decode("ascii").lower().rstrip(".") for d in config.FETCH_ALLOWED_DOMAINS}
    if allowed and host not in allowed:
        raise FetchError(f"域名不在抓取白名单: {host}")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, None, type=socket.SOCK_STREAM
        )
    except socket.gaierror:
        raise FetchError(f"无法解析域名: {host}") from None
    public: list[str] = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if not _is_public_ip(ip):
            raise FetchError(f"目标地址非公网，已拒绝连接: {ip}（域名 {host}）")
        public.append(str(ip))
    if not public:
        raise FetchError(f"目标主机无可用地址: {host}")
    return public[0]


# ---------- 底层 HTTP/1.1 流式读取 ----------


async def _connect(host: str, ip: str, port: int, scheme: str):
    """直连已校验 IP；HTTPS 用原始 host 做 SNI 与证书校验。"""
    ctx = ssl.create_default_context() if scheme == "https" else None
    try:
        return await asyncio.wait_for(
            asyncio.open_connection(
                host=ip, port=port, ssl=ctx, server_hostname=host if ctx else None
            ),
            timeout=config.FETCH_TIMEOUT,
        )
    except (OSError, asyncio.TimeoutError) as e:
        raise FetchError(f"连接失败（{host}:{port}）: {e}") from e


async def _read_headers(reader, initial: bytes = b"") -> tuple[int, dict[str, str], bytes]:
    data = initial
    while b"\r\n\r\n" not in data:
        if len(data) > _MAX_HEADER_BYTES:
            raise FetchError("响应头过大")
        try:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=config.FETCH_TIMEOUT)
        except asyncio.TimeoutError:
            raise FetchError("读取响应头超时") from None
        if not chunk:
            raise FetchError("响应头读取失败（连接提前关闭）")
        data += chunk
    header_block, _, rest = data.partition(b"\r\n\r\n")
    if len(header_block) + 4 > _MAX_HEADER_BYTES:
        raise FetchError("响应头过大")
    lines = header_block.split(b"\r\n")
    parts = lines[0].split(b" ")
    if len(parts) < 2 or parts[0] not in (b"HTTP/1.0", b"HTTP/1.1") or len(parts[1]) != 3 or not parts[1].isdigit():
        raise FetchError("响应状态行非法")
    status = int(parts[1])
    if not 100 <= status <= 599:
        raise FetchError("响应状态行非法")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        k, sep, v = line.partition(b":")
        if not sep or not _HEADER_NAME.fullmatch(k) or any(c < 32 and c != 9 for c in v):
            raise FetchError("响应头字段非法")
        key = k.decode("ascii").lower()
        value = v.decode("latin-1").strip()
        if key in headers:
            if key == "content-length":
                if headers[key] != value:
                    raise FetchError("冲突的 Content-Length")
            else:
                headers[key] += "," + value
        else:
            headers[key] = value
    return status, headers, rest


async def _read_body(reader, headers: dict[str, str], initial: bytes) -> bytes:
    limit = config.FETCH_MAX_BYTES
    te = headers.get("transfer-encoding", "").lower()
    cl_raw = headers.get("content-length")
    expected = None

    if te and cl_raw is not None:
        raise FetchError("响应同时包含 Transfer-Encoding 和 Content-Length")
    if cl_raw is not None:
        if not cl_raw.isascii() or not cl_raw.isdigit():
            raise FetchError("非法 Content-Length")
        normalized = cl_raw.lstrip("0") or "0"
        budget = str(limit)
        if len(normalized) > len(budget) or (len(normalized) == len(budget) and normalized > budget):
            raise FetchError(f"响应体过大（Content-Length 超过 {limit} 字节）已提前拒绝")
        expected = int(normalized)

    if te:
        if te.strip() != "chunked":
            raise FetchError("不支持的 Transfer-Encoding")
        return await _read_chunked(reader, initial, limit)
    return await _read_fixed(reader, initial, limit, expected)


async def _read_fixed(reader, initial: bytes, limit: int, expected: int | None) -> bytes:
    if len(initial) > limit:
        raise FetchError(f"响应体超过大小上限（{limit} 字节），已中止")
    if expected is not None and len(initial) > expected:
        raise FetchError("响应体超过 Content-Length")
    body = bytearray(initial)
    total = len(initial)
    while True:
        if expected is not None and total == expected:
            break
        try:
            remaining = (expected if expected is not None else limit) - total
            chunk = await asyncio.wait_for(reader.read(min(65536, remaining + 1)), timeout=config.FETCH_TIMEOUT)
        except asyncio.TimeoutError:
            raise FetchError("读取响应体超时") from None
        if not chunk:
            if expected is not None:
                raise FetchError("响应体不完整（未达到 Content-Length）")
            break  # 无 Content-Length 时，EOF 正常结束响应。
        total += len(chunk)
        if total > limit:
            raise FetchError(f"响应体超过大小上限（{limit} 字节），已中止")
        if expected is not None and total > expected:
            raise FetchError("响应体超过 Content-Length")
        body.extend(chunk)
    return bytes(body)


async def _read_chunked(reader, initial: bytes, limit: int) -> bytes:
    buf = initial
    body = bytearray()

    async def read_more(n: int):
        nonlocal buf
        try:
            chunk = await asyncio.wait_for(reader.read(n), timeout=config.FETCH_TIMEOUT)
        except asyncio.TimeoutError:
            raise FetchError("读取响应体超时") from None
        if not chunk:
            raise FetchError("chunked 响应不完整")
        buf += chunk
        if len(buf) > limit + _MAX_HEADER_BYTES:
            raise FetchError("chunked 响应超过大小上限")

    async def read_line() -> bytes:
        nonlocal buf
        while b"\r\n" not in buf:
            if len(buf) > _MAX_HEADER_BYTES:
                raise FetchError("chunk 头过大")
            await read_more(4096)
        line, _, buf = buf.partition(b"\r\n")
        if len(line) > _MAX_HEADER_BYTES:
            raise FetchError("chunk 头过大")
        return line

    if len(initial) > limit + _MAX_HEADER_BYTES:
        raise FetchError("chunked 响应超过大小上限")
    while True:
        line = await read_line()
        size_str = line.split(b";")[0].strip()
        if not size_str or not all(c in b"0123456789abcdefABCDEF" for c in size_str):
            raise FetchError("非法 chunk 大小")
        size = int(size_str, 16)
        if size == 0:
            trailer_bytes = 0
            while True:
                trailer = await read_line()
                trailer_bytes += len(trailer) + 2
                if trailer_bytes > _MAX_HEADER_BYTES:
                    raise FetchError("chunked 尾部过大")
                if not trailer:
                    break
                key, sep, _ = trailer.partition(b":")
                if not sep or not _HEADER_NAME.fullmatch(key) or key.lower() in (b"content-length", b"transfer-encoding"):
                    raise FetchError("chunked 尾部非法")
            if buf:
                raise FetchError("chunked 结束后存在多余数据")
            break
        if len(body) + size > limit:
            raise FetchError(f"响应体超过大小上限（{limit} 字节），已中止")
        while len(buf) < size + 2:
            await read_more(min(65536, size + 2 - len(buf)))
        if buf[size:size + 2] != b"\r\n":
            raise FetchError("chunked 数据分隔符非法")
        body.extend(buf[:size])
        buf = buf[size + 2:]
    return bytes(body)


def _decompress_gzip(data: bytes) -> bytes:
    """边解压边限制解压后大小，防止压缩炸弹。"""
    limit = config.FETCH_MAX_BYTES
    if not data:
        raise FetchError("gzip 响应不完整")
    out = bytearray()
    pending = data
    try:
        while pending:  # gzip 允许多个连续成员，所有成员共用同一输出预算。
            d = zlib.decompressobj(16 + zlib.MAX_WBITS)
            while not d.eof:
                if not pending:
                    raise FetchError("gzip 响应不完整")
                block, pending = pending[:65536], pending[65536:]
                while block:
                    piece = d.decompress(block, min(65536, limit - len(out) + 1))
                    if len(out) + len(piece) > limit:
                        raise FetchError(f"解压后响应体超过大小上限（{limit} 字节），已中止")
                    out.extend(piece)
                    if d.eof:
                        pending = d.unused_data + pending
                        break
                    block = d.unconsumed_tail
    except zlib.error as e:
        raise FetchError("gzip 响应损坏") from e
    return bytes(out)


async def _request_once(url: str) -> tuple[int, dict[str, str], bytes]:
    """单次请求含 DNS、连接与正文处理的总时间也受超时限制。"""
    try:
        return await asyncio.wait_for(_request_once_impl(url), timeout=config.FETCH_TIMEOUT)
    except asyncio.TimeoutError:
        raise FetchError("链接抓取超时") from None
    except (OSError, UnicodeError) as e:
        raise FetchError(f"链接抓取失败：{type(e).__name__}") from e


async def _request_once_impl(url: str) -> tuple[int, dict[str, str], bytes]:
    """对单个 URL 发一次 GET（直连已校验 IP），返回 (status, headers, body)。"""
    parsed = _parse_url(url)
    if parsed is None:
        raise FetchError("仅支持无账号密码的 http/https 链接")
    scheme, host, port = parsed
    raw = urlsplit(url)
    target = quote(raw.path or "/", safe="/:@!$&'()*+,;=-._~%")
    if raw.query:
        target += "?" + quote(raw.query, safe="/?:@!$&'()*+,;=-._~%")
    authority = f"[{host}]" if ":" in host else host
    if port != (443 if scheme == "https" else 80):
        authority += f":{port}"
    ip = await _resolve_public_ip(host)
    reader, writer = await _connect(host, ip, port, scheme)
    try:
        req = (
            f"GET {target} HTTP/1.1\r\n"
            f"Host: {authority}\r\n"
            f"User-Agent: {_UA}\r\n"
            "Accept: text/html,text/plain,application/json,application/xml;q=0.9,*/*;q=0.1\r\n"
            "Accept-Encoding: gzip\r\n"
            "Connection: close\r\n\r\n"
        )
        writer.write(req.encode("ascii"))
        await writer.drain()
        status, headers, rest = await _read_headers(reader)
        informational = 0
        while 100 <= status < 200:
            informational += 1
            if status == 101 or informational > 5:
                raise FetchError("不支持的临时 HTTP 响应")
            status, headers, rest = await _read_headers(reader, rest)
        body = b"" if status in (204, 304) else await _read_body(reader, headers, rest)
        if headers.get("content-encoding", "").lower() == "gzip":
            body = _decompress_gzip(body)
        return status, headers, body
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=config.FETCH_TIMEOUT)
        except Exception:
            pass


# ---------- 公开入口 ----------


def _extract_text(body: bytes, content_type: str, source_url: str) -> str:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype not in _ALLOWED_CTYPES:
        raise FetchError(f"不支持的响应类型: {ctype or '未知'}")
    text = body.decode("utf-8", errors="replace")
    if ctype in ("text/html", "application/xhtml+xml"):
        soup = BeautifulSoup(text, "html.parser")
        for tag in soup(["script", "style", "noscript", "iframe"]):
            tag.decompose()
        text = soup.get_text("\n", strip=True)
    if len(text) > config.FETCH_TEXT_LIMIT:
        text = text[: config.FETCH_TEXT_LIMIT]
    logger.info("抓取成功: %s（%d 字符）", source_url, len(text))
    return text


async def fetch_text(url: str) -> str:
    """抓取网页正文文本（含手动重定向跟随与逐跳校验）。

    Args:
        url: 初始链接。

    Returns:
        正文纯文本（已截断到 FETCH_TEXT_LIMIT）。

    Raises:
        FetchError: 任何抓取失败 / 安全拒绝，带用户可读原因。
    """
    text, _ = await fetch_text_with_headers(url)
    return text


async def fetch_text_with_headers(url: str) -> tuple[str, dict[str, str]]:
    """抓取网页正文，同时返回响应头（含 ETag / Last-Modified）。

    用于公共来源变化检测：上游（crawler）可结合 ETag、Last-Modified
    与正文哈希判断“是否实质变化”，只有实质变化才调模型生成草稿。

    Returns:
        (正文纯文本, 响应头 dict，键名小写)。

    Raises:
        FetchError: 任何抓取失败 / 安全拒绝，带用户可读原因。
    """
    async with _sem:
        current = url
        for hop in range(config.FETCH_MAX_REDIRECTS + 1):
            parsed = _parse_url(current)
            if parsed is None:
                raise FetchError("仅支持无账号密码的 http/https 链接")
            status, headers, body = await _request_once(current)
            if status in (301, 302, 303, 307, 308):
                location = headers.get("location")
                if not location:
                    raise FetchError("重定向响应缺少 Location")
                current = urljoin(current, location)
                logger.info("跟随重定向(%d/%d): %s", hop + 1, config.FETCH_MAX_REDIRECTS, current)
                continue
            if status >= 400:
                raise FetchError(f"HTTP 状态码 {status}")
            text = _extract_text(body, headers.get("content-type", ""), current)
            return text, headers
        raise FetchError(f"重定向次数超过上限（{config.FETCH_MAX_REDIRECTS}）")
