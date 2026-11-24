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
from infra.database import get_db

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


REAL_REQUESTS = page.Requests
REAL_SETTLED = fill._settled


class Requests:
    async def start(self, _endpoint):
        return None

    def submitted(self, _now):
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
    _clear_marks()

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

    class Cdp:
        """The page's CDP connection: it answers only the frame tree, whose document is the current loader."""

        def __init__(self, _endpoint) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def call(self, method, **_params):
            assert method == "Page.getFrameTree"
            return {"frameTree": {"frame": {"loaderId": w.loaders[0], "url": f"{SITE}/login"}}}

    async def command(_owner, _runtime, _lease, args, _timeout):
        w.steps.append(args[0])

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
    monkeypatch.setattr(browser_cdp, "CdpClient", Cdp)
    monkeypatch.setattr(page, "linked_windows", linked)
    monkeypatch.setattr(page, "find_form", find)
    monkeypatch.setattr(page, "type_values", type_values)
    monkeypatch.setattr(page, "submit", submit)
    monkeypatch.setattr(page, "Requests", Requests)
    monkeypatch.setattr(fill, "_settled", settled)
    yield w
    _clear_marks()


def _clear_marks() -> None:
    for held in (browser_fill_marks._scripted, browser_fill_marks._session_marks, browser_fill_marks._site_marks,
                 browser_control._saved_fills):
        held.clear()


def _never_shown(w, caplog, *texts: str) -> None:
    """The saved value is in no returned text, log record, person-step instruction or module global."""
    held = [caplog.text, *texts, *w.owner.declared,
            *(repr(vars(module)) for module in (fill, page, browser_fill_marks, browser_control, saved_login_fills))]
    assert not any(w.sentinel in text for text in held)


def _results(monkeypatch) -> list[str]:
    called: list[str] = []
    for name in ("fill_succeeded", "fill_failed", "fill_unknown"):
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
    assert text == fill.REFUSED["no_grant"].format(site=SITE) and "read" not in world.steps and not world.typed


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
async def test_a_result_the_page_never_showed_records_it_as_unknown(world, monkeypatch):
    called = _results(monkeypatch)
    world.outcome = "unknown"
    assert await fill.fill_saved_login(THREAD, world.login_id) == fill.UNKNOWN.format(site=SITE, username=USER)
    assert called == ["fill_unknown"]


def _audit(login_id: str) -> list[str]:
    db = get_db()
    try:
        return [row[0] for row in db.execute(
            "SELECT result FROM saved_login_fill_audit WHERE login_id = ?", (login_id,)).fetchall()]
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["unknown", "unsubmitted", "type_changed", "type_raises", "submit_raises",
                                 "settle_raises"])
async def test_every_end_after_typing_began_without_a_known_result_writes_one_unknown_row(world, monkeypatch, end):
    async def raises(*_args):
        raise RuntimeError("down")

    def answers(status):
        async def answer(*_args):
            return status
        return answer

    if end == "unknown":
        world.outcome = "unknown"
    elif end == "unsubmitted":
        monkeypatch.setattr(page, "submit", answers("unsubmitted"))
    elif end == "type_changed":
        monkeypatch.setattr(page, "type_values", answers("changed"))
    elif end == "type_raises":
        monkeypatch.setattr(page, "type_values", raises)
    elif end == "submit_raises":
        monkeypatch.setattr(page, "submit", raises)
    else:
        monkeypatch.setattr(fill, "_settled", raises)
    called = _results(monkeypatch)
    await fill.fill_saved_login(THREAD, world.login_id)
    assert called == ["fill_unknown"] and _audit(world.login_id) == ["unknown"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("outcome", "row"), [("signed_in", "filled"), ("rejected", "rejected")])
async def test_a_known_result_writes_only_its_own_row(world, outcome, row):
    world.outcome = outcome
    await fill.fill_saved_login(THREAD, world.login_id)
    assert _audit(world.login_id) == [row]


@pytest.mark.asyncio
async def test_nothing_typed_writes_no_row(world):
    world.grants = [True, False]
    await fill.fill_saved_login(THREAD, world.login_id)
    assert _audit(world.login_id) == []


@pytest.mark.asyncio
async def test_a_failed_unknown_record_keeps_the_fills_own_line(world, monkeypatch, caplog):
    world.outcome = "unknown"

    def broken(*_args):
        raise ValueError(f"no login {world.sentinel}")

    monkeypatch.setattr(saved_login_results, "fill_unknown", broken)
    text = await fill.fill_saved_login(THREAD, world.login_id)
    assert text == fill.UNKNOWN.format(site=SITE, username=USER)
    _never_shown(world, caplog, text)


@pytest.mark.asyncio
async def test_a_login_no_longer_saved_says_so(world, monkeypatch):
    def gone(*_args, **_kwargs):
        raise ValueError("not saved")

    monkeypatch.setattr(saved_logins, "get_login", gone)
    assert await fill.fill_saved_login(THREAD, world.login_id) == fill.REFUSED["gone"]


@pytest.mark.asyncio
async def test_a_get_sign_in_form_is_refused_before_any_read_and_is_the_persons_step(world, caplog):
    world.form = "get_form"
    text = await fill.fill_saved_login(THREAD, world.login_id)
    lead = fill.REFUSED["get_form"].format(site=SITE)
    assert text == f"{lead} {browser_control.PERSON_STEP}{browser_control.seen_by_person(THREAD)}"
    assert world.owner.declared == [f"Sign in to {SITE} yourself"]
    assert "read" not in world.steps and "mark" not in world.steps and not world.typed
    _never_shown(world, caplog, text)


# Every status _fill can end in: find_form's refusals, type_values's and the fill's own.
STATUSES = ["signed_in", "rejected", "not_clean", "unknown", "no_field", "http_frame", "frame_origin",
            "form_action", "insecure_action", "get_form", "changed", "no_grant", "page", "unsubmitted"]


@pytest.mark.asyncio
async def test_every_status_has_its_own_line_and_none_falls_through(world):
    login = saved_logins.get_login(world.login_id)
    lines = {}
    for status in STATUSES:
        browser_control._saved_fills.clear()
        lines[status] = await fill._result(world.owner, THREAD, login, status)
    assert len(set(lines.values())) == len(STATUSES), lines
    assert all("{" not in line for line in lines.values()), lines
    with pytest.raises(KeyError):
        await fill._result(world.owner, THREAD, login, "a-status-with-no-line")


@pytest.mark.asyncio
async def test_a_document_an_agent_scripted_is_the_persons_sign_in_with_no_reload(world, caplog):
    # The agent's eval marks this document and its site, as before_agent_command does for any agent script.
    await browser_fill_marks.before_agent_command(THREAD, "s1", ENDPOINT, ("eval", "document.title"))
    assert browser_fill_marks.scripted(THREAD, "L1") and browser_fill_marks.site_marked(THREAD, "s1", "example.com")
    text = await fill.fill_saved_login(THREAD, world.login_id)
    assert text == f"{fill.NOT_CLEAN.format(site=SITE)} {browser_control.PERSON_STEP}{browser_control.seen_by_person(THREAD)}"
    assert "reload" not in world.steps and "read" not in world.steps and not world.typed
    _never_shown(world, caplog, text)


@pytest.mark.asyncio
@pytest.mark.parametrize("dirty", ["route", "script_on_another_page_of_the_site", "linked_window"])
async def test_a_marked_or_linked_page_is_the_persons_step_with_no_reload(world, dirty, caplog):
    if dirty == "route":
        await browser_fill_marks.before_agent_command(THREAD, "s1", ENDPOINT, ("network", "route", "*"))
    elif dirty == "script_on_another_page_of_the_site":
        # The script ran in an earlier document of the saved site; this one is new, but the site is marked.
        world.loaders = ["L0"]
        await browser_fill_marks.before_agent_command(THREAD, "s1", ENDPOINT, ("eval", "1"))
        world.loaders = ["L1"]
    else:
        world.linked = True
    text = await fill.fill_saved_login(THREAD, world.login_id)
    lead = fill.NOT_CLEAN.format(site=SITE)
    assert text == f"{lead} {browser_control.PERSON_STEP}{browser_control.seen_by_person(THREAD)}"
    assert "reload" not in world.steps and "read" not in world.steps and not world.typed
    _never_shown(world, caplog, text)


@pytest.mark.asyncio
@pytest.mark.parametrize("steps", [
    (f"open {SITE}/account", "eval window.addEventListener('submit', () => {})"),
    ("tab 2", "eval window.addEventListener('submit', () => {})"),
    ("click @e4", "wait --fn window.ready"),
])
async def test_a_batch_that_runs_script_after_moving_documents_leaves_the_sign_in_to_the_person(world, steps, caplog):
    # The batch's script may run in a document opened or switched to by an earlier step, so the session is marked.
    await browser_fill_marks.before_agent_command(THREAD, "s1", ENDPOINT, ("batch", *steps))
    text = await fill.fill_saved_login(THREAD, world.login_id)
    assert text == f"{fill.NOT_CLEAN.format(site=SITE)} {browser_control.PERSON_STEP}{browser_control.seen_by_person(THREAD)}"
    assert "read" not in world.steps and not world.typed
    _never_shown(world, caplog, text)


@pytest.mark.asyncio
async def test_a_requests_watch_that_fails_to_start_still_closes_its_socket(world, monkeypatch, caplog):
    closed = []

    class Failing:
        def __init__(self, _endpoint) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            closed.append(True)

        async def call(self, method, **_params):
            assert method == "Network.enable"
            raise BrowserSessionError(page.PAGE_CHECK_FAILED)

    monkeypatch.setattr(page, "Requests", REAL_REQUESTS)
    monkeypatch.setattr(page, "CdpClient", Failing)
    text = await fill.fill_saved_login(THREAD, world.login_id)
    assert text == fill.REFUSED["page"].format(site=SITE)
    assert closed == [True] and "read" not in world.steps and not world.typed
    _never_shown(world, caplog, text)


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["no_field", "http_frame", "frame_origin", "form_action", "insecure_action"])
async def test_a_form_that_fails_a_check_is_refused_with_a_fixed_line_before_anything_is_read(world, refusal):
    world.form = refusal
    text = await fill.fill_saved_login(THREAD, world.login_id)
    assert text == fill.REFUSED[refusal].format(site=SITE, username=USER)
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


ONE = {"passwords": 1, "form": True, "user": True, "action": "https://example.com/session", "method": "post"}


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
    ([("https://example.com", {**ONE, "action": "http://example.com/session"})], "insecure_action"),
    ([("https://example.com", {**ONE, "action": "http://login.example.com/session"})], "insecure_action"),
    ([("https://example.com", {**ONE, "action": "http://example.net/session"})], "form_action"),
    ([("https://example.com", {**ONE, "action": "javascript:void(0)"})], "form_action"),
    ([("https://example.com", {**ONE, "method": "get"})], "get_form"),
    ([("https://example.com", {**ONE, "method": "dialog"})], page.SignInForm(100, True)),
    ([("https://example.com", {**ONE, "action": "https://example.net/p", "method": "get"})], "form_action"),
    ([("https://example.com", {**ONE, "form": False, "action": "", "method": ""})], page.SignInForm(100, True)),
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

    async def probe(_endpoint, _known):
        visit, host, shown = pending.pop(0) if len(pending) > 1 else pending[0]
        return browser_cdp.PasswordProbe(visit, host, frozenset({1}) if shown else frozenset(), "T1",
                                         present_nodes=frozenset())

    monkeypatch.setattr(browser_cdp, "page_password_probe", probe)
    tracker = page.Requests()
    tracker.seen = requests_seen
    assert await fill._settled(ENDPOINT, "example.com", tracker) == expected


def _sent(tracker: page.Requests, request: str, now: float, kind: str = "XHR") -> None:
    tracker.note({"method": "Network.requestWillBeSent", "params": {"requestId": request, "type": kind}}, now)


def _finished(tracker: page.Requests, request: str, now: float) -> None:
    tracker.note({"method": "Network.loadingFinished", "params": {"requestId": request}}, now)


def test_only_sign_in_requests_started_since_the_submit_count_and_stay_open_until_they_finish():
    tracker = page.Requests()
    _sent(tracker, "poll", 9.0)
    tracker.submitted(10.0)
    _sent(tracker, "sign-in", 10.5)
    _sent(tracker, "image", 10.5, kind="Image")
    assert tracker.seen == 1 and tracker.open_since_submit()
    # However long it has been open: no time turns a sign-in request into background traffic.
    _finished(tracker, "sign-in", 500.0)
    assert not tracker.open_since_submit()
    assert tracker.seen == 1


async def _settle(monkeypatch, tracker: page.Requests, probes, on_probe=None) -> str:
    """_settled with fast timings; `on_probe(count, tracker, now)` runs before each probe answers."""
    monkeypatch.setattr(browser_step_lifecycle, "REPROBE_AFTER_INPUT", 0.01)
    monkeypatch.setattr(browser_step_lifecycle, "REPROBE_EVERY", 0.03)
    monkeypatch.setattr(fill, "SETTLE_LIMIT", 0.3)
    pending, count = list(probes), [0]

    async def probe(_endpoint, _known):
        count[0] += 1
        if on_probe is not None:
            on_probe(count[0], tracker, asyncio.get_running_loop().time())
        visit, host, shown = pending.pop(0) if len(pending) > 1 else pending[0]
        return browser_cdp.PasswordProbe(visit, host, frozenset({1}) if shown else frozenset(), "T1",
                                         present_nodes=frozenset())

    monkeypatch.setattr(browser_cdp, "page_password_probe", probe)
    return await fill._settled(ENDPOINT, "example.com", tracker)


def _submitted_with_sign_in_request() -> page.Requests:
    tracker = page.Requests()
    now = asyncio.get_running_loop().time()
    tracker.submitted(now)
    _sent(tracker, "sign-in", now)
    return tracker


@pytest.mark.asyncio
@pytest.mark.parametrize("shown", [True, False])
async def test_a_sign_in_request_still_open_at_the_limit_leaves_the_result_unknown(monkeypatch, shown):
    """Whether or not the password field still shows: a page that would read signed in waits on it too."""
    tracker = _submitted_with_sign_in_request()
    assert await _settle(monkeypatch, tracker, [("L1", "example.com", shown)]) == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize(("shown", "expected"), [(True, "rejected"), (False, "signed_in")])
async def test_a_sign_in_request_that_finishes_lets_the_page_decide(monkeypatch, shown, expected):
    tracker = _submitted_with_sign_in_request()

    def answer_on_the_third_probe(count, held, now):
        if count == 3:
            _finished(held, "sign-in", now)

    assert await _settle(monkeypatch, tracker, [("L1", "example.com", shown)], answer_on_the_third_probe) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("shown", "expected"), [(True, "rejected"), (False, "signed_in")])
async def test_a_request_the_page_started_before_the_submit_doesnt_hold_the_result(monkeypatch, shown, expected):
    tracker = page.Requests()
    now = asyncio.get_running_loop().time()
    _sent(tracker, "poll", now - 1)
    tracker.submitted(now)
    _sent(tracker, "sign-in", now)
    _finished(tracker, "sign-in", now)
    assert await _settle(monkeypatch, tracker, [("L1", "example.com", shown)]) == expected


@pytest.mark.asyncio
async def test_a_slow_correct_sign_in_marks_nothing_out_of_date_and_records_unknown_once(world, monkeypatch, caplog):
    """The sign-in request is still open at the settle limit: neither result is recorded, only fill_unknown."""
    called = _results(monkeypatch)

    class SlowServer(REAL_REQUESTS):
        async def start(self, _endpoint):
            return None

        async def stop(self):
            return None

        def submitted(self, now):
            super().submitted(now)
            _sent(self, "sign-in", now)

    monkeypatch.setattr(page, "Requests", SlowServer)
    monkeypatch.setattr(fill, "_settled", REAL_SETTLED)
    monkeypatch.setattr(browser_step_lifecycle, "REPROBE_AFTER_INPUT", 0.01)
    monkeypatch.setattr(browser_step_lifecycle, "REPROBE_EVERY", 0.03)
    monkeypatch.setattr(fill, "SETTLE_LIMIT", 0.3)

    async def probe(_endpoint, _known):
        # The sign-in page still shows its password field: only the open request keeps this from 'rejected'.
        return browser_cdp.PasswordProbe("L1", "example.com", frozenset({1}), "T1", present_nodes=frozenset())

    monkeypatch.setattr(browser_cdp, "page_password_probe", probe)
    text = await fill.fill_saved_login(THREAD, world.login_id)
    assert text == fill.UNKNOWN.format(site=SITE, username=USER)
    assert called == ["fill_unknown"]
    assert saved_logins.get_login(world.login_id).status != saved_logins.STATUS_OUT_OF_DATE
    assert world.owner.declared == [] and world.typed
    _never_shown(world, caplog, text)
