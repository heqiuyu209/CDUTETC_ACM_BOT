"""web_scraping.py 的离线测试。

抓取脚本依赖 Playwright / lxml / openai，属于可选依赖；未安装时整组跳过，
不影响机器人主体的离线测试。
"""

import pytest

pytest.importorskip("requests")
pytest.importorskip("lxml")
pytest.importorskip("openai")
pytest.importorskip("playwright.sync_api")

import web_scraping as ws


class FakePage:
    def __init__(self, url, html):
        self.url = url
        self._html = html
        self.goto_url = None

    def goto(self, url, **kwargs):
        self.goto_url = url

    def wait_for_timeout(self, timeout):
        pass

    def content(self):
        return self._html


class FakeBrowser:
    def __init__(self, page):
        self._page = page
        self.closed = False

    def new_page(self):
        return self._page

    def close(self):
        self.closed = True


class FakePlaywright:
    def __init__(self, browser):
        self.chromium = self
        self._browser = browser

    def launch(self, **kwargs):
        return self._browser

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _install_fake_browser(monkeypatch, page_url, html):
    page = FakePage(page_url, html)
    browser = FakeBrowser(page)
    monkeypatch.setattr(ws, "sync_playwright", lambda: FakePlaywright(browser))
    return page, browser


def _capture_links(monkeypatch):
    """替换模型调用，记录传给模型的链接列表。"""
    calls = []

    def fake_chat(system_prompt, query):
        calls.append(query)
        return '{"useful_links": []}'

    monkeypatch.setattr(ws, "deepseek_chat", fake_chat)
    return calls


def test_relative_links_use_redirected_page_url(monkeypatch):
    """首页重定向到 /2026/ 后，notice.html 应解析到跳转后的目录。"""
    page, browser = _install_fake_browser(
        monkeypatch,
        "https://example.com/2026/",
        '<html><body><a href="notice.html">报名通知</a></body></html>',
    )
    calls = _capture_links(monkeypatch)

    assert ws.get_all_links_playwright("https://example.com/") == []

    assert page.goto_url == "https://example.com/"
    assert browser.closed is True
    links = calls[0]
    assert any(link.endswith(" | https://example.com/2026/notice.html") for link in links)
    assert not any(link.endswith(" | https://example.com/notice.html") for link in links)


def test_no_redirect_uses_requested_url(monkeypatch):
    """未发生跳转时，仍以请求地址解析相对链接。"""
    _install_fake_browser(
        monkeypatch,
        "https://example.com/",
        '<html><body><a href="notice.html">报名通知</a></body></html>',
    )
    calls = _capture_links(monkeypatch)

    ws.get_all_links_playwright("https://example.com/")

    assert any(link.endswith(" | https://example.com/notice.html") for link in calls[0])


def test_empty_page_url_falls_back_to_requested_url(monkeypatch):
    """异常情况下 page.url 为空时回退到原始 URL，避免解析失败。"""
    _install_fake_browser(
        monkeypatch,
        "",
        '<html><body><a href="notice.html">报名通知</a></body></html>',
    )
    calls = _capture_links(monkeypatch)

    ws.get_all_links_playwright("https://example.com/")

    assert any(link.endswith(" | https://example.com/notice.html") for link in calls[0])


def test_missing_url_returns_empty():
    assert ws.get_all_links_playwright("") == []
