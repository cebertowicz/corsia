import pytest

from api import browser
from host_access import browser_control, browser_pane
from ws import browser_stream


@pytest.mark.parametrize(("address", "expected"), [
    (" example.com/path ", "https://example.com/path"),
    ("example.com:8080", "https://example.com:8080"),
    ("localhost:3000", "http://localhost:3000"),
    ("service.localhost", "http://service.localhost"),
    ("localhost.:3000", "http://localhost.:3000"),
    ("127.0.0.1", "http://127.0.0.1"),
    ("127.0.0.2:80", "http://127.0.0.2:80"),
    ("127.1", "http://127.1"),
    ("2130706433", "http://2130706433"),
    ("[::1]:3000", "http://[::1]:3000"),
    ("http://example.com", "http://example.com"),
    ("HTTPS://example.com", "HTTPS://example.com"),
    ("javascript:alert(1)", None),
    ("file:///tmp/x", None),
    ("data:text/plain,hello", None),
    ("chrome://settings", None),
    ("two words", None),
    ("intranet", None),
    ("https:", None),
    ("https://[broken", None),
])
def test_normalize_browser_url(address, expected):
    assert browser_control.normalize_browser_url(address) == expected


@pytest.mark.asyncio
async def test_api_open_normalizes_before_navigation(monkeypatch):
    opened = []
    monkeypatch.setattr(browser, "_owned_thread", lambda *_args: {"id": "user"})
    monkeypatch.setattr(browser, "emit_product_event", lambda *_args, **_kwargs: None)

    async def open_url(thread_id, url):
        opened.append((thread_id, url))
        return "ok"

    monkeypatch.setattr(browser_pane, "open_url", open_url)
    assert await browser.open_url("thread", "example.com", "Bearer test") == {"opened": True}
    assert opened == [("thread", "https://example.com")]


@pytest.mark.asyncio
async def test_stream_normalizes_before_navigation(monkeypatch):
    opened = []

    async def open_url(thread_id, url, connection_token):
        opened.append((thread_id, url, connection_token))
        return "ok"

    async def page_info(_thread_id):
        return "https://example.com", "Example"

    monkeypatch.setattr(browser_pane, "open_url", open_url)
    monkeypatch.setattr(browser_pane, "page_info", page_info)
    result = await browser_stream._navigate("thread", "connection", {"url": "example.com"})
    assert opened == [("thread", "https://example.com", "connection")]
    assert result["url"] == "https://example.com"
    assert result["error"] is None
