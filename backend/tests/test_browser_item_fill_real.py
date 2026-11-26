"""The saved-item page half on real Chrome: item_field's checks and fill_item's write, run as written.

Runs only when CORSIA_TEST_CHROME names a Chrome or Chromium binary (as test_browser_frame_password_real.py). The
fixture site is plain http on 127.0.0.1, so each test admits that one origin through the fill's single https check
(browser_login_page.https_url), as the saved-login e2e does.

The last test runs the whole agent path on the native browser (CORSIA_BROWSER_E2E=1, as test_browser_login_fill_e2e):
an item filled and sent to a page that echoes it back, where a screenshot is refused, and a page without it, where one
is taken. CI runs this file in browser-native.yml.
"""
from __future__ import annotations

import html
import json
import re
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs

import pytest

from host_access import browser_cdp, browser_item_fill, browser_login_page as page, browser_saved_fills
from infra import key_store, saved_item_fills
from tests.test_browser_frame_password_real import CHROME, _chrome, _until
from tests.test_browser_login_fill_e2e import _admit_fixture, _agent, browser  # noqa: F401 - browser is a fixture

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not CHROME, reason="set CORSIA_TEST_CHROME to a Chrome binary")]

VALUE = "4417 1234 5678 9113"
PAGE = """<!doctype html><title>Portal</title>
<form method=post action=/save><input id=text type=text><input id=pw type=password><input id=tel type=tel>
<input id=flip type=password onfocus="this.type = 'text'"><button>Save</button></form>
<form method=get action=/find><input id=get type=text></form>
<form method=post action=https://collect.example.net/p><input id=away type=text></form>
<input id=loose type=text><div id=box></div>
<script>window.events = [];
for (const el of document.querySelectorAll('input'))
  for (const name of ['input', 'change']) el.addEventListener(name, () => events.push(el.id + ':' + name));</script>"""


@contextmanager
def _site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler protocol name
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(PAGE.encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()


@pytest.fixture
def portal(monkeypatch):
    monkeypatch.setattr(page, "https_url", lambda url: url if isinstance(url, str) and url.startswith("http") else None)
    with _site() as origin, _chrome(f"{origin}/portal.html") as endpoint:
        yield origin, endpoint


async def _node(cdp, element: str) -> int:
    # Chrome lists the page before it finishes loading; a document read mid-load is replaced, and its ids with it.
    await _until(lambda: _loaded(cdp))
    document = await cdp.call("DOM.getDocument", depth=0)
    found = await cdp.call("DOM.querySelector", nodeId=document["root"]["nodeId"], selector=f"#{element}")
    return (await cdp.call("DOM.describeNode", nodeId=found["nodeId"]))["node"]["backendNodeId"]


async def _loaded(cdp) -> bool | None:
    return True if await _read(cdp, "document.readyState === 'complete' && Array.isArray(window.events)") else None


async def _read(cdp, expression: str):
    return (await cdp.call("Runtime.evaluate", expression=expression, returnByValue=True))["result"]["value"]


async def _check(cdp, origin: str, element: str, site: str | None = None):
    return await page.item_field(cdp, None, await _node(cdp, element), site or origin, lambda url: url.rstrip("/"))


@pytest.mark.asyncio
async def test_a_text_and_a_password_field_are_filled_with_their_events(portal):
    origin, endpoint = portal
    async with browser_cdp.CdpClient(endpoint) as cdp:
        for element in ("text", "pw", "loose"):
            field = await _check(cdp, origin, element)
            assert isinstance(field, page.ItemField), field
            assert await page.fill_item(cdp, field, VALUE) == "typed"
            assert await _read(cdp, f"document.getElementById({json.dumps(element)}).value") == VALUE
        assert await _read(cdp, "events") == [f"{e}:{n}" for e in ("text", "pw", "loose") for n in ("input", "change")]


@pytest.mark.asyncio
@pytest.mark.parametrize(("element", "refusal"), [
    ("tel", "not_field"), ("box", "not_field"), ("get", "get_form"), ("away", "form_action"),
])
async def test_other_fields_and_forms_are_refused_and_left_empty(portal, element, refusal):
    origin, endpoint = portal
    async with browser_cdp.CdpClient(endpoint) as cdp:
        assert await _check(cdp, origin, element) == refusal
        assert await _read(cdp, "events") == []


@pytest.mark.asyncio
async def test_another_site_is_refused(portal):
    origin, endpoint = portal
    async with browser_cdp.CdpClient(endpoint) as cdp:
        assert await _check(cdp, origin, "text", site="http://localhost:1") == "frame_origin"


@pytest.mark.asyncio
async def test_a_field_that_changes_when_focused_is_not_filled(portal):
    origin, endpoint = portal
    async with browser_cdp.CdpClient(endpoint) as cdp:
        field = await _check(cdp, origin, "flip")
        assert isinstance(field, page.ItemField)
        assert await page.fill_item(cdp, field, VALUE) == "changed"
        assert await _read(cdp, "document.getElementById('flip').value") == ""


@pytest.mark.asyncio
async def test_a_form_that_moves_after_the_check_is_not_filled(portal):
    origin, endpoint = portal
    async with browser_cdp.CdpClient(endpoint) as cdp:
        field = await _check(cdp, origin, "text")
        await _read(cdp, "document.forms[0].action = 'https://collect.example.net/p'; true")
        assert await page.fill_item(cdp, field, VALUE) == "changed"
        assert await _read(cdp, "document.getElementById('text').value") == ""


NAME, LABEL = "member-number", "Member number"
FORM = """<!doctype html><title>Member</title><form method=post action=/save>
<input id=key type=text name=key><button>Save</button></form>"""
PLAIN = "<!doctype html><title>Plain</title><main><p>Nothing saved here.</p></main>"


@contextmanager
def _echo_site():
    """A form page, the page its POST lands on (which shows what was sent, as a settings page shows a key) and a
    page that shows nothing saved."""
    class Handler(BaseHTTPRequestHandler):
        def _send(self, body: str) -> None:
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler protocol name
            self._send(FORM if self.path.startswith("/form.html") else PLAIN)

        def do_POST(self):  # noqa: N802
            sent = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()).get("key", [""])[0]
            self._send(f"<!doctype html><title>Saved</title><main><p>Your member number: {html.escape(sent)}</p></main>")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


async def _on(run, title: str) -> bool | None:
    return True if title in await _agent(run, "get", "title") else None


@pytest.mark.asyncio
async def test_a_screenshot_of_a_page_that_shows_a_filled_item_is_refused_and_of_one_that_doesnt_is_taken(
        browser, monkeypatch, tmp_path):  # noqa: F811 - the imported fixture
    with _echo_site() as origin:
        site_origin = _admit_fixture(monkeypatch, origin)
        key_store.add_entry(NAME, LABEL, "", added_by_thread_id=None, added_by_name=None)
        key_store.set_value(NAME, browser.sentinel)
        monkeypatch.setattr(saved_item_fills, "granted_site", lambda _thread, _name: site_origin)
        try:
            await _agent(browser, "open", f"{origin}/form.html")
            text = await browser_item_fill.fill_saved_item(browser.thread, NAME, "#key")
            browser.outputs.append(text)
            assert text == browser_item_fill.FILLED.format(label=LABEL, site=site_origin), text
            # On the filled page an element is named by its snapshot ref; the button sends the form.
            button = re.search(r'button "Save" \[ref=(e\d+)\]', await _agent(browser, "snapshot", "-i"))
            assert button, browser.outputs[-1]
            await _agent(browser, "click", f"@{button.group(1)}")
            await _until(lambda: _on(browser, "Saved"))
            shown = tmp_path / "shown.png"
            refused = await _agent(browser, "screenshot", str(shown))
            assert browser_saved_fills.SCREENSHOT_SHOWS_SAVED_VALUE in refused, refused
            assert not shown.exists()
            await _agent(browser, "open", f"{origin}/plain.html")
            plain = tmp_path / "plain.png"
            taken = await _agent(browser, "screenshot", str(plain))
            assert not taken.startswith("Error"), taken
            assert plain.exists() and plain.stat().st_size > 0
        finally:
            await browser.owner.shutdown()
