"""The page resolver and the capture's page watch read the browser's pages through one reader (#3307)."""
from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from host_access import browser_cdp, browser_cdp_targets, browser_login_capture_writes as capture_writes
from host_access.browser_session_state import BrowserSessionError

LISTING = [
    {"id": "tab-1", "type": "page", "url": "https://one.test/", "title": "One",
     "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/page/tab-1"},
    {"id": "frame-1", "type": "iframe", "url": "https://frame.test/", "webSocketDebuggerUrl": "ws://127.0.0.1/f"},
    {"id": "worker-1", "type": "service_worker", "webSocketDebuggerUrl": "ws://127.0.0.1/w"},
    "not a target",
    {"id": "tab-2", "type": "page", "url": "https://two.test/", "title": "Two",
     "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/page/tab-2"},
]


def _serve(body: bytes) -> Iterator[str]:
    class Listing(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's handler name
            self.send_response(200 if self.path == "/json/list" else 404)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Listing)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"ws://127.0.0.1:{server.server_address[1]}/devtools/browser/b-1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def browser() -> Iterator[str]:
    yield from _serve(json.dumps(LISTING).encode())


@pytest.fixture(params=[b'{"not": "a list"}', b"not json"], ids=["not-a-list", "not-json"])
def bad_browser(request: pytest.FixtureRequest) -> Iterator[str]:
    yield from _serve(request.param)


def test_the_resolver_and_the_watch_see_the_same_pages(browser):
    pages = browser_cdp_targets.page_targets(browser)
    assert [page["id"] for page in pages] == ["tab-1", "tab-2"]
    assert capture_writes._page_ids(browser, 3) == frozenset({"tab-1", "tab-2"})
    by_endpoint = {page["webSocketDebuggerUrl"]: page["id"] for page in pages}
    assert by_endpoint[browser_cdp._page_endpoint(browser)] in capture_writes._page_ids(browser, 3)
    assert browser_cdp._page_endpoint(browser, "https://two.test/", "Two") == "ws://127.0.0.1/devtools/page/tab-2"


def test_a_bad_listing_is_refused_for_both(bad_browser):
    with pytest.raises(BrowserSessionError, match=browser_cdp_targets.PAGES_UNREADABLE):
        browser_cdp._page_endpoint(bad_browser)
    # The watch treats any raise from its page read as "every document is marked" (rule 7 tests).
    with pytest.raises(BrowserSessionError):
        capture_writes._page_ids(bad_browser, 3)


def test_an_unreachable_browser_is_refused_for_both():
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)  # bound, then closed: nothing listens
    endpoint = f"ws://127.0.0.1:{server.server_address[1]}/devtools/browser/gone"
    server.server_close()
    with pytest.raises(BrowserSessionError):
        browser_cdp._page_endpoint(endpoint)
    with pytest.raises(BrowserSessionError):
        capture_writes._page_ids(endpoint, 1)
