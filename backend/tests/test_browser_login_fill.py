"""The saved-login fill executor's decisions, with the page and the browser session faked.

Real Chrome is in test_browser_login_fill_e2e.py. These drive `fill_saved_login` against a real saved login in
an isolated database, so the approval, the read of the saved value and the result functions are Governance's
own; only the page (browser_login_page), the browser session and the settle are stand-ins.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from types import SimpleNamespace

import pytest

from host_access import (
    browser_cdp, browser_control, browser_fill_marks, browser_login_fill as fill, browser_login_page as page,
    browser_session_commands, browser_step_lifecycle,
)
from host_access.browser_session_state import BrowserSessionError
from infra import key_store_values, saved_login_fills, saved_login_results, saved_logins

THREAD = "thread-fill"
SITE = "https://example.com"
USER = "me@example.com"
ENDPOINT = "ws://127.0.0.1:9/devtools/page/T1"


class Owner:
    def __init__(self, declare: BaseException | None = None) -> None:
        self.runtime = SimpleNamespace(action_lock=asyncio.Lock(), record=SimpleNamespace(session="s1"))
        self.declared: list[str] = []
        self.declare = declare

    async def _pin_native(self) -> None:
        return None

    async def _admit(self, _thread_id, **_kwargs):
        return self.runtime

    async def declare_person_step(self, _thread_id, instruction):
        self.declared.append(instruction)
        if self.declare is not None:
            raise self.declare


class Cdp:
    def __init__(self, _endpoint) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class Requests:
    async def start(self, _endpoint):
        return None

    async def stop(self):
        return None


@pytest.fixture
def world(isolated_db, tmp_path, monkeypatch, caplog):
    """A saved login, a faked page that fills cleanly, and a record of every step in order."""
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    sentinel = f"Sentinel-{uuid.uuid4().hex}"
    login_id = saved_logins.save_login(site=SITE, username=USER, password=sentinel, saved_by="person")
    caplog.set_level(logging.DEBUG)
    w = SimpleNamespace(login_id=login_id, sentinel=sentinel, owner=Owner(), steps=[], typed=[], loaders=["L1"],
                        grants=[True], form=page.SignInForm(7, True), outcome="signed_in", linked=False)
    for held in (browser_fill_marks._scripted, browser_fill_marks._session_marks, browser_control._saved_fills):
        held.clear()

    def may_fill(thread_id, login):
        w.steps.append("may_fill")
        return w.grants.pop(0) if len(w.grants) > 1 else w.grants[0]

    real_resolve, real_mark = key_store_values.resolve, browser_control.mark_saved_fill

    def resolve(*args, **kwargs):
        w.steps.append("read")
        return real_resolve(*args, **kwargs)

    def mark(*args):
        w.steps.append("mark")
        real_mark(*args)

    async def endpoint(_command):
        return ENDPOINT

    async def browser(_command, _refusal):
        return "ws://127.0.0.1:9/devtools/browser/B"

    async def loader(_cdp):
        return w.loaders[0]

    async def command(_owner, _runtime, _lease, args, _timeout):
        w.steps.append(args[0])
        if args == ("reload",):
            w.loaders.pop(0) if len(w.loaders) > 1 else None

    async def find(_cdp, site, _normalize):
        w.steps.append("find")
        return w.form

    async def type_values(_cdp, _form, username, value):
        w.steps.append("type")
        w.typed.append((username, value))
        return "typed"

    async def submit(_cdp, _form):
        w.steps.append("submit")
        return "enter"

    async def settled(_endpoint, _domain, _requests):
        return w.outcome

    async def linked(_cdp, _target):
        return w.linked

    monkeypatch.setattr(fill, "_owner", lambda: w.owner)
    monkeypatch.setattr(saved_login_fills, "may_fill", may_fill)
    monkeypatch.setattr(key_store_values, "resolve", resolve)
    monkeypatch.setattr(browser_control, "mark_saved_fill", mark)
    monkeypatch.setattr(browser_cdp, "page_endpoint", endpoint)
    monkeypatch.setattr(browser_cdp, "browser_endpoint", browser)
    monkeypatch.setattr(browser_session_commands, "_command", command)
    monkeypatch.setattr(page, "QuietCdp", Cdp)
    monkeypatch.setattr(page, "main_loader", loader)
    monkeypatch.setattr(page, "linked_windows", linked)
    monkeypatch.setattr(page, "find_form", find)
    monkeypatch.setattr(page, "type_values", type_values)
    monkeypatch.setattr(page, "submit", submit)
    monkeypatch.setattr(page, "Requests", Requests)
    monkeypatch.setattr(fill, "_settled", settled)
    yield w
    for held in (browser_fill_marks._scripted, browser_fill_marks._session_marks, browser_control._saved_fills):
        held.clear()


def _never_shown(w, caplog, *texts: str) -> None:
    """The saved value is in no returned text, log record, person-step instruction or module global."""
    held = [caplog.text, *texts, *w.owner.declared,
            *(repr(vars(module)) for module in (fill, page, browser_fill_marks, browser_control, saved_login_fills))]
    assert not any(w.sentinel in text for text in held)


def _results(monkeypatch) -> list[str]:
    called: list[str] = []
    for name in ("fill_succeeded", "fill_failed"):
        def recording(*a, _real=getattr(saved_login_results, name), _name=name):
            called.append(_name)
            return _real(*a)
        monkeypatch.setattr(saved_login_results, name, recording)
    return called


@pytest.mark.asyncio
async def test_without_a_live_approval_the_person_is_asked_and_nothing_is_filled(world, monkeypatch, caplog):
    world.grants = [False]
    asked = []
    monkeypatch.setattr(saved_login_fills, "request_fill", lambda *args: asked.append(args) or {})
    text = await fill.fill_saved_login(THREAD, world.login_id)
    assert text == fill.ASKED.format(site=SITE, username=USER)
    assert asked == [(THREAD, world.login_id)] and "read" not in world.steps and not world.typed
    _never_shown(world, caplog, text)


@pytest.mark.asyncio
async def test_an_out_of_date_login_gives_the_person_its_sign_in_with_the_approved_refusal(world, caplog):
    world.grants = [False]
    saved_logins.mark_out_of_date(world.login_id)
    text = await fill.fill_saved_login(THREAD, world.login_id)
    refusal = f"The saved login for {SITE} ({USER}) is out of date. The person has a step to sign in to {SITE} themselves."
    assert text == f"{refusal} {browser_control.PERSON_STEP}{browser_control.seen_by_person(THREAD)}"
    assert world.owner.declared == [f"Sign in to {SITE} yourself"] and not world.typed
    _never_shown(world, caplog, text)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [BrowserSessionError(browser_control.DECLARATION_NOT_RECORDED),
                                     BrowserSessionError(browser_control.PASSWORD_PAGE_UNRECORDED), RuntimeError("down")])
async def test_a_person_step_that_cant_be_recorded_says_so(world, failure):
    world.grants = [False]
    saved_logins.mark_out_of_date(world.login_id)
    world.owner.declare = failure
    assert await fill.fill_saved_login(THREAD, world.login_id) == browser_control.DECLARATION_NOT_RECORDED


@pytest.mark.asyncio
async def test_a_step_already_open_on_the_sign_in_page_counts_as_the_persons_step(world):
    world.grants = [False]
    saved_logins.mark_out_of_date(world.login_id)
    world.owner.declare = BrowserSessionError(browser_control.PASSWORD_PAGE + " The page is open.")
    text = await fill.fill_saved_login(THREAD, world.login_id)
    assert text.endswith(browser_control.PERSON_STEP + browser_control.seen_by_person(THREAD))


@pytest.mark.asyncio
async def test_the_document_and_session_are_marked_and_the_approval_checked_just_before_the_value_is_read(world, caplog):
    text = await fill.fill_saved_login(THREAD, world.login_id)
    assert world.steps[world.steps.index("read") - 1] == "may_fill"
    assert world.steps[-5:] == ["mark", "may_fill", "read", "type", "submit"]
    assert world.typed == [(USER, world.sentinel)]
    # The mark names the document checked before typing: only it gets the filled-field allowlist.
    assert browser_control.saved_fill_session(THREAD, "s1")
    assert browser_control._saved_fill_document(THREAD, "s1", world.loaders[0])
    assert not browser_control._saved_fill_document(THREAD, "s1", "a-later-document")
    _never_shown(world, caplog, text)


@pytest.mark.asyncio
async def test_an_approval_that_ends_before_typing_types_nothing(world, caplog):
    world.grants = [True, False]
    text = await fill.fill_saved_login(THREAD, world.login_id)
    assert text == fill.REFUSED["no_grant"] and "read" not in world.steps and not world.typed


@pytest.mark.asyncio
async def test_a_sign_in_that_worked_calls_only_fill_succeeded(world, monkeypatch, caplog):
    called = _results(monkeypatch)
    text = await fill.fill_saved_login(THREAD, world.login_id)
    assert called == ["fill_succeeded"]
    assert text == f"Signed in to {SITE} as {USER} with the saved login."
    _never_shown(world, caplog, text)


@pytest.mark.asyncio
async def test_a_rejected_sign_in_calls_only_fill_failed_and_gives_the_person_the_sign_in(world, monkeypatch, caplog):
    called = _results(monkeypatch)
    world.outcome = "rejected"
    text = await fill.fill_saved_login(THREAD, world.login_id)
    assert called == ["fill_failed"]
    rejected = (f"The saved login for {SITE} ({USER}) didn't work, so it's now marked out of date. "
                f"The person has a step to sign in to {SITE} themselves.")
    assert text == f"{rejected} {browser_control.PERSON_STEP}{browser_control.seen_by_person(THREAD)}"
    assert saved_logins.get_login(world.login_id).status == saved_logins.STATUS_OUT_OF_DATE
    assert world.owner.declared == [f"Sign in to {SITE} yourself"]
    _never_shown(world, caplog, text)


@pytest.mark.asyncio
async def test_a_result_the_page_never_showed_calls_neither_result_function(world, monkeypatch):
    called = _results(monkeypatch)
    world.outcome = "unknown"
    assert await fill.fill_saved_login(THREAD, world.login_id) == fill.UNKNOWN.format(site=SITE)
    assert called == []


@pytest.mark.asyncio
async def test_a_document_an_agent_scripted_is_reloaded_once_and_then_filled(world):
    world.loaders = ["L1", "L2"]
    browser_fill_marks._mark(THREAD, "L1")
    await fill.fill_saved_login(THREAD, world.login_id)
    assert world.steps.count("reload") == 1 and world.typed


@pytest.mark.asyncio
@pytest.mark.parametrize("dirty", ["route", "script_after_reload", "linked_window"])
async def test_a_document_still_not_clean_after_one_reload_is_the_persons_step(world, dirty, caplog):
    if dirty == "route":
        browser_fill_marks._session_marks.add(f"{THREAD}:s1")
    elif dirty == "script_after_reload":
        browser_fill_marks._mark(THREAD, "L1")
    else:
        world.linked = True
    text = await fill.fill_saved_login(THREAD, world.login_id)
    lead = fill.NOT_CLEAN.format(site=SITE)
    assert text == f"{lead} {browser_control.PERSON_STEP}{browser_control.seen_by_person(THREAD)}"
    assert world.steps.count("reload") == 1 and "read" not in world.steps and not world.typed
    _never_shown(world, caplog, text)


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["no_field", "http_frame", "frame_origin", "form_action"])
async def test_a_form_that_fails_a_check_is_refused_with_a_fixed_line_before_anything_is_read(world, refusal):
    world.form = refusal
    text = await fill.fill_saved_login(THREAD, world.login_id)
    assert text == fill.REFUSED[refusal].format(site=SITE)
    assert "read" not in world.steps and "mark" not in world.steps and not world.typed


@pytest.mark.asyncio
async def test_no_open_browser_fills_nothing(world, monkeypatch):
    async def closed(*_args, **_kwargs):
        raise BrowserSessionError("the browser is no longer open")

    monkeypatch.setattr(world.owner, "_admit", closed)
    assert await fill.fill_saved_login(THREAD, world.login_id) == fill.NO_BROWSER


# find_form: the frame and form checks, with the isolated-world script's answer faked per frame.
class FrameCdp:
    def __init__(self, frames: list[tuple[str, dict]]) -> None:
        self.frames = frames

    async def call(self, method, **params):
        if method == "Page.getFrameTree":
            (origin, _), *children = self.frames
            child = [{"frame": {"id": f"F{i}", "securityOrigin": o}} for i, (o, _) in enumerate(children, 1)]
            return {"frameTree": {"frame": {"id": "F0", "securityOrigin": origin}, "childFrames": child}}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": int(params["frameId"][1:]) + 100}
        if method == "Runtime.evaluate":
            return {"result": {"value": self.frames[params["contextId"] - 100][1]}}
        raise AssertionError(method)


ONE = {"passwords": 1, "form": True, "user": True, "action": "https://example.com/session"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("frames", "expected"), [
    ([("https://example.com", ONE)], page.SignInForm(100, True)),
    ([("https://example.com", {"passwords": 0}), ("https://example.com", ONE)], page.SignInForm(101, False)),
    ([("https://example.com", {"passwords": 0})], "no_field"),
    ([("https://example.com", ONE), ("https://example.com", ONE)], "no_field"),
    ([("http://example.com", ONE)], "http_frame"),
    ([("https://login.example.com", ONE)], "frame_origin"),
    ([("https://example.net", ONE)], "frame_origin"),
    ([("https://example.com", {**ONE, "action": "https://collect.example.net/p"})], "form_action"),
    ([("https://example.com", {**ONE, "action": "http://example.com/session"})], "form_action"),
    ([("https://example.com", {**ONE, "action": "javascript:void(0)"})], "form_action"),
    ([("https://example.com", {**ONE, "form": False, "action": ""})], page.SignInForm(100, True)),
])
async def test_the_field_must_be_in_an_https_frame_of_the_saved_site_and_its_form_must_send_there(frames, expected):
    assert await page.find_form(FrameCdp(frames), SITE, saved_logins.normalized_site) == expected


# _settled: the password step's settle, with the page's probes and requests faked.
@pytest.mark.asyncio
@pytest.mark.parametrize(("probes", "requests_seen", "expected"), [
    ([("L1", "example.com", True)] * 2 + [("L2", "example.com", False)] * 9, 1, "signed_in"),
    ([("L1", "example.com", True)] * 12, 1, "rejected"),
    ([("L1", "example.com", True)] * 2 + [("L2", "example.com", True)] * 9, 1, "rejected"),
    ([("L1", "example.com", True)] * 40, 0, "unknown"),
    ([("L1", "example.com", True)] * 2 + [("L2", "idp.example.net", False)] * 9, 1, "unknown"),
])
async def test_the_result_waits_for_the_page_to_settle_as_the_password_step_does(monkeypatch, probes, requests_seen, expected):
    monkeypatch.setattr(browser_step_lifecycle, "REPROBE_AFTER_INPUT", 0.01)
    monkeypatch.setattr(browser_step_lifecycle, "REPROBE_EVERY", 0.03)
    monkeypatch.setattr(fill, "SETTLE_LIMIT", 0.3)
    pending = list(probes)

    async def probe(_endpoint, _known, *, client=None):
        assert client is page.QuietCdp
        visit, host, shown = pending.pop(0) if len(pending) > 1 else pending[0]
        return browser_cdp.PasswordProbe(visit, host, frozenset({1}) if shown else frozenset(), "T1",
                                         present_nodes=frozenset())

    monkeypatch.setattr(browser_cdp, "page_password_probe", probe)
    tracker = page.Requests()
    tracker.seen = requests_seen
    assert await fill._settled(ENDPOINT, "example.com", tracker) == expected


def test_a_request_still_open_holds_the_settle_until_it_counts_as_background():
    tracker = page.Requests()
    tracker.note({"method": "Network.requestWillBeSent", "params": {"requestId": "1", "type": "XHR"}}, 10.0)
    tracker.note({"method": "Network.requestWillBeSent", "params": {"requestId": "2", "type": "Image"}}, 10.0)
    assert tracker.seen == 1 and tracker.in_flight(11.0)
    assert not tracker.in_flight(10.0 + page.BACKGROUND_AFTER)
    tracker.note({"method": "Network.loadingFinished", "params": {"requestId": "1"}}, 11.0)
    assert not tracker.in_flight(11.0)
