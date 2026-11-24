"""Real-Chrome proof of saved-login capture: the person's own sign-in through the pane input path.

Run through the isolated CORSIA test environment:
CORSIA_BROWSER_E2E=1 ... python -m pytest backend/tests/test_browser_login_capture_e2e.py -m integration

The fixture server is plain http on 127.0.0.1, so each test lets that one origin pass the https-only site check
(capture._secure_site) and nothing else; every other step is the product's own.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs
from uuid import uuid4

import pytest

from host_access import browser_cdp, browser_control, browser_login_capture as capture, browser_pane
from host_access import browser_step_lifecycle as lifecycle
from host_access.browser_session_state import BrowserSessionError
from host_access.browser_sessions import BrowserSessionOwner
from tests.test_browser_login_capture import Holder
from tests.test_browser_shared_e2e import _quiet_password_steps

# What the person types: a unique value that must reach offer_login and nowhere else.
MARKER = "zq-marker-5d0e2a41-only-in-the-offer"
WRONG = "zq-wrong-entry-9b31"
ACCOUNT = "person@example.com"

LOGIN = """<!doctype html><title>Sign in</title>
<form method=post action=/login>
<input id=user name=user type=email autocomplete=username>
<input id=pw name=pw type=password autocomplete=current-password>
<button type=submit>Sign in</button>
</form>"""
# A sign-in whose password field shows only once its username has a value, in the same document.
TWO_STEP = """<!doctype html><title>Sign in</title>
<form method=post action=/login>
<input id=user name=user type=email autocomplete=username>
<input id=pw name=pw type=password autocomplete=current-password hidden>
<button type=submit>Sign in</button>
</form>
<script>user.addEventListener('input', () => { pw.hidden = false; });</script>"""
WELCOME = """<!doctype html><title>Welcome</title><p>Signed in</p>"""


@contextmanager
def sign_in_server():
    pages = {"/login.html": LOGIN, "/two.html": TWO_STEP, "/welcome.html": WELCOME}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler protocol name
            body = pages.get(self.path)
            if body is None:
                self.send_error(404)
                return
            self._page(body)

        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler protocol name
            form = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode())
            if form.get("pw") == [MARKER]:
                self.send_response(303)
                self.send_header("Location", "/welcome.html")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._page(LOGIN)  # A failed sign-in comes back with its password field.

        def _page(self, body: str) -> None:
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

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


@pytest.fixture(autouse=True)
def _fresh_state():
    for held in (lifecycle._steps, lifecycle._answers, lifecycle._sessions, lifecycle._probe_locks,
                 browser_control._guarded_visits, capture._pending, capture._marks):
        held.clear()
    yield
    for held in (lifecycle._steps, lifecycle._answers, lifecycle._sessions, lifecycle._probe_locks,
                 browser_control._guarded_visits, capture._pending, capture._marks):
        held.clear()


class Person:
    """The person at the pane: each input goes through the pane's capture hook, then to the page."""

    def __init__(self, owner: BrowserSessionOwner, thread_id: str, endpoint: str) -> None:
        self.owner, self.thread_id, self.endpoint = owner, thread_id, endpoint

    async def _send(self, kind: str, event: dict) -> None:
        await browser_pane.before_person_input(self.thread_id, kind, event, "pane")
        dispatch = browser_cdp.dispatch_mouse_event if kind == "input_mouse" else browser_cdp.dispatch_key_event
        await dispatch(self.endpoint, event)

    async def click(self, selector: str) -> None:
        box = await page_value(self.endpoint, f"(() => {{ const b = document.querySelector('{selector}').getBoundingClientRect();"
                                              " return {x: b.x + b.width / 2, y: b.y + b.height / 2}; })()")
        for kind, button, count in (("mouseMoved", "none", 0), ("mousePressed", "left", 1), ("mouseReleased", "left", 1)):
            await self._send("input_mouse", {"eventType": kind, "x": box["x"], "y": box["y"], "button": button, "clickCount": count})

    async def type(self, text: str) -> None:
        for letter in text:
            for kind in ("keyDown", "keyUp"):
                # A punctuation key's own code point can be a control key's virtual key code ('.' is Delete's).
                key_code = ord(letter.upper()) if letter.isalnum() else 0
                await self._send("input_keyboard", {"eventType": kind, "key": letter, "code": "", "keyCode": key_code,
                                                    "text": letter if kind == "keyDown" else "", "modifiers": 0})

    async def enter(self) -> None:
        for kind in ("keyDown", "keyUp"):
            await self._send("input_keyboard", {"eventType": kind, "key": "Enter", "code": "Enter", "keyCode": 13,
                                                "text": "\r" if kind == "keyDown" else "", "modifiers": 0})


async def page_value(endpoint: str, expression: str):
    async with browser_cdp.CdpClient(endpoint) as cdp:
        result = await cdp.call("Runtime.evaluate", expression=expression, returnByValue=True)
    return result["result"].get("value")


async def wait_for_new_document(endpoint: str, path: str) -> None:
    """Wait until the page is a freshly loaded `path` (the old document carried window.__old)."""
    for _ in range(100):
        try:
            if await page_value(endpoint, f"location.pathname === '{path}' && !window.__old && document.readyState === 'complete'"):
                return
        except (BrowserSessionError, OSError):
            pass
        await asyncio.sleep(0.05)
    raise AssertionError(f"{path} never loaded")


@contextmanager
def wired(monkeypatch, owner: BrowserSessionOwner, origin: str):
    """The offer holder stub, the pane's page endpoint and session identity, and the one http origin allowed."""
    holder = Holder()
    monkeypatch.setitem(sys.modules, capture.OFFERS_MODULE, holder.module)
    monkeypatch.setattr(capture, "_secure_site", lambda site: site == origin)
    monkeypatch.setattr(browser_pane, "session_identity", owner.session_identity)

    async def pane_endpoint(thread_id, _token, _error):
        return await owner.page_endpoint(thread_id)

    monkeypatch.setattr(browser_pane, "_page_endpoint", pane_endpoint)
    yield holder


def _skip_without_browser() -> None:
    if os.environ.get("CORSIA_BROWSER_E2E") != "1":
        pytest.skip("set CORSIA_BROWSER_E2E=1 in the isolated host browser environment")


async def _open_sign_in(owner: BrowserSessionOwner, thread_id: str, url: str) -> str:
    assert not (await owner.run(thread_id, ("open", url), show_window=False, session="new")).startswith("Error:")
    with pytest.raises(BrowserSessionError, match="asks for a password"):
        await owner.run(thread_id, ("snapshot", "-i"), session="existing")
    assert lifecycle.password_step(thread_id) is not None
    return await owner.page_endpoint(thread_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_persons_sign_in_is_offered_then_confirmed_and_the_value_goes_nowhere_else(monkeypatch, caplog):
    _skip_without_browser()
    caplog.set_level("DEBUG")
    thread_id = f"login-capture-{uuid4().hex}"
    owner = BrowserSessionOwner(namespace="corsia-login-capture", instance_key=uuid4().hex)
    _quiet_password_steps(monkeypatch, [])
    browser_control.pane_connected(thread_id, "pane")
    try:
        with sign_in_server() as origin, wired(monkeypatch, owner, origin) as holder:
            endpoint = await _open_sign_in(owner, thread_id, f"{origin}/login.html")
            step = lifecycle.password_step(thread_id)
            key = f"{thread_id}:{owner.session_identity(thread_id)}"
            person = Person(owner, thread_id, endpoint)
            await person.click("#user")
            await person.type(ACCOUNT)
            await person.click("#pw")
            await person.type(MARKER)
            await page_value(endpoint, "window.__old = true")
            await person.enter()
            await wait_for_new_document(endpoint, "/welcome.html")
            # The agent's next command probes the new page: the field is gone, so the step settles.
            assert "Signed in" in await owner.run(thread_id, ("snapshot",), session="existing")
            assert holder.calls == [("offer_login", key, origin, ACCOUNT, MARKER), ("confirm_offer", key)]
            assert MARKER not in caplog.text
            assert MARKER not in repr(vars(step))
            assert MARKER not in repr(capture._pending) + repr(capture._marks)
    finally:
        browser_control.pane_disconnected(thread_id, "pane")
        await owner.shutdown()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_failed_sign_in_that_returns_its_password_field_drops_the_offer(monkeypatch):
    _skip_without_browser()
    thread_id = f"login-capture-failed-{uuid4().hex}"
    owner = BrowserSessionOwner(namespace="corsia-login-capture", instance_key=uuid4().hex)
    _quiet_password_steps(monkeypatch, [])
    browser_control.pane_connected(thread_id, "pane")
    try:
        with sign_in_server() as origin, wired(monkeypatch, owner, origin) as holder:
            endpoint = await _open_sign_in(owner, thread_id, f"{origin}/login.html")
            key = f"{thread_id}:{owner.session_identity(thread_id)}"
            person = Person(owner, thread_id, endpoint)
            await person.click("#user")
            await person.type(ACCOUNT)
            await person.click("#pw")
            await person.type(WRONG)
            await page_value(endpoint, "window.__old = true")
            await person.enter()
            await wait_for_new_document(endpoint, "/login")
            with pytest.raises(BrowserSessionError, match="asks for a password"):
                await owner.run(thread_id, ("snapshot", "-i"), session="existing")
            assert holder.calls == [("offer_login", key, origin, ACCOUNT, WRONG), ("drop_offer", key)]
    finally:
        browser_control.pane_disconnected(thread_id, "pane")
        await owner.shutdown()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_agents_fill_on_the_username_means_no_offer(monkeypatch):
    _skip_without_browser()
    thread_id = f"login-capture-agent-{uuid4().hex}"
    owner = BrowserSessionOwner(namespace="corsia-login-capture", instance_key=uuid4().hex)
    _quiet_password_steps(monkeypatch, [])
    browser_control.pane_connected(thread_id, "pane")
    try:
        with sign_in_server() as origin, wired(monkeypatch, owner, origin) as holder:
            assert not (await owner.run(thread_id, ("open", f"{origin}/two.html"), show_window=False, session="new")).startswith("Error:")
            snapshot = await owner.run(thread_id, ("snapshot", "-i"), session="existing")
            refs = re.findall(r"textbox \[ref=(e\d+)\]", snapshot)
            assert refs, snapshot
            # The agent types the username; the page then shows its password field in the same document.
            assert await owner.run(thread_id, ("fill", f"@{refs[0]}", ACCOUNT), session="existing") == "ok"
            with pytest.raises(BrowserSessionError, match="asks for a password"):
                await owner.run(thread_id, ("snapshot", "-i"), session="existing")
            endpoint = await owner.page_endpoint(thread_id)
            person = Person(owner, thread_id, endpoint)
            await person.click("#pw")
            await person.type(MARKER)
            await person.enter()
            assert holder.calls == []
    finally:
        browser_control.pane_disconnected(thread_id, "pane")
        await owner.shutdown()
