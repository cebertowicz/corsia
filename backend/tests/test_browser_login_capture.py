"""Saved-login capture: the person's own submit hands a login to the offer holder, and nothing else does.

Each test names the Governance rule it proves (rules 1-8 in the Browser lane's capture brief).
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import re
import threading
import types
from types import SimpleNamespace

import pytest
from fastapi import WebSocketDisconnect

from host_access import browser_login_capture as capture, browser_pane, browser_session_commands, browser_steps
from host_access import browser_step_lifecycle as lifecycle
from host_access.browser_cdp import PasswordProbe
from host_access.browser_cli import CommandResult
from host_access.browser_control import PageAccess
from infra import saved_login_offers
from tests.test_browser_pane import FakePane, screencast  # noqa: F401 - autouse fixture: no real page screencast
from ws import browser_stream

THREAD = "capture-thread"
KEY = f"{THREAD}:session-1"
SITE = "https://accounts.example.com"
# A unique value the person types; it must reach offer_login and nowhere else.
MARKER = "zq-marker-7c41e0b9-only-in-the-offer"


class Holder:
    """The offer holder's four calls, stubbed on the real module and recorded; offer_login answers `offered`."""

    def __init__(self, monkeypatch) -> None:
        self.calls: list[tuple] = []
        self.offered = True
        self.offer_threads: list[int] = []
        for name in ("offer_login", "confirm_offer", "drop_offer", "note_username"):
            monkeypatch.setattr(saved_login_offers, name, self._recorder(name))

    def _recorder(self, name):
        def record(*args):
            self.calls.append((name, *args))
            if name == "offer_login":
                self.offer_threads.append(threading.get_ident())
                return self.offered
            return None
        return record

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


class FakePage:
    """A page target: frames with their security origins, and fields as node -> (frame, typed, username)."""

    def __init__(self, *, loader="doc-1", frames=(("top", SITE),), fields=None, accounts=None, hidden=()):
        self.loader, self.frames = loader, list(frames)
        self.fields = dict(fields or {})
        self.accounts = dict(accounts or {})
        self.hidden = set(hidden)
        self.calls: list[tuple[str, dict]] = []
        self.hang = False

    def client(self, _endpoint: str) -> FakeCdp:
        return FakeCdp(self)


class FakeCdp:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.contexts: dict[int, str] = {}

    async def __aenter__(self) -> FakeCdp:
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def call(self, method: str, **params):
        page = self.page
        page.calls.append((method, params))
        if page.hang:
            await asyncio.Future()
        if method == "Page.getFrameTree":
            (top, origin), *children = page.frames
            return {"frameTree": {
                # The page-reported URL deliberately differs from the security origin.
                "frame": {"id": top, "loaderId": page.loader, "securityOrigin": origin, "url": "https://reported.example/"},
                "childFrames": [{"frame": {"id": frame, "securityOrigin": child_origin}} for frame, child_origin in children]}}
        if method == "Page.createIsolatedWorld":
            assert params["worldName"] == "corsia-login-capture" and params["grantUniveralAccess"] is False
            context = 100 + len(self.contexts)
            self.contexts[context] = params["frameId"]
            return {"executionContextId": context}
        if method == "DOM.getBoxModel":
            node = params["backendNodeId"]
            width = 0 if node in page.hidden or node not in page.fields else 40
            return {"model": {"border": [0, 0, width, 0, width, 10, 0, 10]}}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": f"{params['backendNodeId']}:{params['executionContextId']}"}}
        if method == "Runtime.callFunctionOn":
            node, context = (int(part) for part in params["objectId"].split(":"))
            frame, typed, username = page.fields[node]
            return {"result": {"value": [typed, username] if frame == self.contexts[context] else None}}
        if method == "Runtime.evaluate":
            return {"result": {"value": page.accounts.get(self.contexts[params["contextId"]])}}
        raise AssertionError(method)


@pytest.fixture
def holder(monkeypatch) -> Holder:
    return Holder(monkeypatch)


@pytest.fixture(autouse=True)
def _clean_capture_state():
    capture._pending.clear()
    capture._marks.clear()
    capture._groups.clear()
    capture._flights.clear()
    capture._tails.clear()
    capture._items.clear()
    yield
    capture._pending.clear()
    capture._marks.clear()
    capture._groups.clear()
    capture._flights.clear()
    capture._tails.clear()


async def drain_capture() -> None:
    while capture._tasks:
        await asyncio.gather(*list(capture._tasks))


async def settled_submit(thread_id: str, session_key: str, endpoint: str) -> None:
    await capture.person_submit(thread_id, session_key, endpoint)
    await drain_capture()


def use_page(monkeypatch, page: FakePage) -> FakePage:
    monkeypatch.setattr(capture, "_QuietCdp", page.client)
    return page


def open_step(monkeypatch, nodes=frozenset({7}), visit="doc-1") -> browser_steps.PasswordStep:
    step = browser_steps.PasswordStep(visit, "accounts.example.com", "ws://page", session="session-1", nodes=nodes)
    monkeypatch.setitem(lifecycle._steps, THREAD, step)
    return step


def probe(visit="doc-2", host="accounts.example.com", visible=frozenset()) -> PasswordProbe:
    return PasswordProbe(visit, host, visible, "ws://page", present_nodes=visible)


# Rule 1 -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("kind", "event", "submit"), [
    ("input_mouse", {"eventType": "mousePressed"}, True),
    ("input_mouse", {"eventType": "mouseReleased"}, False),
    ("input_mouse", {"eventType": "mouseMoved"}, False),
    ("input_keyboard", {"eventType": "keyDown", "key": "Enter"}, True),
    ("input_keyboard", {"eventType": "rawKeyDown", "key": "Enter"}, True),
    ("input_keyboard", {"eventType": "keyUp", "key": "Enter"}, False),
    ("input_keyboard", {"eventType": "keyDown", "key": "a"}, False),
    ("clipboard_paste", {"eventType": "keyDown", "key": "Enter"}, False),
])
def test_rule1_only_the_persons_press_or_enter_is_a_submit(kind, event, submit):
    assert capture.is_submit(kind, event) is submit


@pytest.mark.asyncio
async def test_rule1_the_pane_reads_the_submit_before_the_page_receives_it(monkeypatch):
    order, delivered = [], asyncio.Event()
    monkeypatch.setattr(browser_pane, "session_identity", lambda _thread: "session-1")

    async def endpoint(*_args):
        return "ws://page"

    async def submit(thread_id, session_key, page_endpoint):
        order.append(("capture", thread_id, session_key, page_endpoint))

    def deliver(name):
        async def dispatch(_thread_id, data, _token):
            order.append((name, data.get("eventType"), data.get("key")))
            if len(order) == 5:
                delivered.set()
        return dispatch

    monkeypatch.setattr(browser_pane, "_page_endpoint", endpoint)
    monkeypatch.setattr(capture, "person_submit", submit)
    monkeypatch.setattr(browser_pane, "mouse_event", deliver("mouse"))
    monkeypatch.setattr(browser_pane, "key_event", deliver("key"))

    class Pane(FakePane):
        async def receive_text(self):
            if self.messages:
                return self.messages.pop(0)
            await delivered.wait()
            raise WebSocketDisconnect()

    pane = Pane([json.dumps(message) for message in (
        {"type": "input_mouse", "eventType": "mousePressed", "x": 1, "y": 1},
        {"type": "input_keyboard", "eventType": "keyDown", "key": "a"},
        {"type": "input_keyboard", "eventType": "keyDown", "key": "Enter"},
    )])
    with pytest.raises(WebSocketDisconnect):
        await asyncio.wait_for(browser_stream._relay(pane, THREAD, "user"), 2)
    assert order == [
        ("capture", THREAD, KEY, "ws://page"), ("mouse", "mousePressed", None),
        ("key", "keyDown", "a"),
        ("capture", THREAD, KEY, "ws://page"), ("key", "keyDown", "Enter"),
    ]


def test_rule1_only_the_pane_input_path_reaches_the_capture():
    """An agent's Enter or click runs through the command path, which never calls the person's capture."""
    backend = pathlib.Path(__file__).resolve().parents[1]
    callers = {}
    for path in backend.rglob("*.py"):
        if "tests" in path.parts or path.name == "browser_login_capture.py":
            continue
        text = path.read_text(encoding="utf-8")
        for name in ("person_submit", "before_person_input"):
            if re.search(rf"\b{name}\(", text):
                callers.setdefault(name, set()).add(path.relative_to(backend).as_posix())
    assert callers == {"person_submit": {"host_access/browser_pane.py"},
                       "before_person_input": {"host_access/browser_pane.py", "ws/browser_stream.py"}}


@pytest.mark.asyncio
async def test_no_session_identity_means_no_capture(monkeypatch):
    seen = []
    monkeypatch.setattr(browser_pane, "session_identity", lambda _thread: None)

    async def endpoint(*_args):
        seen.append("endpoint")
        return "ws://page"

    async def submit(*args):
        seen.append(args)

    monkeypatch.setattr(browser_pane, "_page_endpoint", endpoint)
    monkeypatch.setattr(capture, "person_submit", submit)
    await browser_pane.before_person_input(THREAD, "input_mouse", {"eventType": "mousePressed"}, "pane")
    assert seen == []


@pytest.mark.asyncio
async def test_a_session_or_page_that_cant_be_found_never_stops_the_persons_input(monkeypatch):
    seen = []

    def no_owner(_thread):
        raise RuntimeError("no browser owner")

    async def submit(*args):
        seen.append(args)

    monkeypatch.setattr(browser_pane, "session_identity", no_owner)
    monkeypatch.setattr(capture, "person_submit", submit)
    await browser_pane.before_person_input(THREAD, "input_keyboard", {"eventType": "keyDown", "key": "Enter"}, "pane")
    assert seen == []


@pytest.mark.asyncio
async def test_no_open_password_step_makes_no_offer(monkeypatch, holder):
    use_page(monkeypatch, FakePage(fields={7: ("top", MARKER, "person@example.com")}))
    await settled_submit(THREAD, KEY, "ws://page")
    assert "offer_login" not in holder.names()


# Rules 2 and 3 ----------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rule2_the_field_and_username_are_read_in_an_isolated_world_of_the_fields_frame(monkeypatch, holder):
    page = use_page(monkeypatch, FakePage(frames=(("top", "https://shell.example.com"), ("child", SITE)),
                                          fields={7: ("child", MARKER, "person@example.com")}))
    open_step(monkeypatch)
    await settled_submit(THREAD, KEY, "ws://page")
    # The site is the field's own frame's security origin, never the page-reported URL.
    assert holder.calls == [("offer_login", KEY, SITE, "person@example.com", MARKER)]
    methods = [method for method, _params in page.calls]
    assert "Runtime.evaluate" not in methods
    assert all("executionContextId" in params for method, params in page.calls if method == "DOM.resolveNode")
    assert {params["frameId"] for method, params in page.calls if method == "Page.createIsolatedWorld"} == {"top", "child"}


@pytest.mark.asyncio
async def test_rule2_an_empty_username_still_offers(monkeypatch, holder):
    use_page(monkeypatch, FakePage(fields={7: ("top", MARKER, "")}))
    open_step(monkeypatch)
    await settled_submit(THREAD, KEY, "ws://page")
    assert holder.calls == [("offer_login", KEY, SITE, "", MARKER)]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["out_of_process", "hidden", "two_values", "empty"])
async def test_rule2_reads_only_the_steps_visible_in_process_fields(monkeypatch, holder, case):
    out_of_process_key = (5 << 32 | 9) << 32 | 7
    nodes = {"out_of_process": frozenset({out_of_process_key}), "two_values": frozenset({7, 8})}.get(case, frozenset({7}))
    fields = {7: ("top", "" if case == "empty" else MARKER, "person"), 8: ("top", "another-value", "person")}
    page = use_page(monkeypatch, FakePage(fields=fields, hidden={7} if case == "hidden" else ()))
    open_step(monkeypatch, nodes=nodes)
    await settled_submit(THREAD, KEY, "ws://page")
    assert holder.calls == []
    if case in {"out_of_process", "hidden"}:
        assert "Runtime.callFunctionOn" not in [method for method, _params in page.calls]


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["http://accounts.example.com", "null", "chrome-extension://abc", ""])
async def test_rule3_a_site_that_isnt_https_is_skipped_unread(monkeypatch, holder, origin):
    page = use_page(monkeypatch, FakePage(frames=(("top", origin),), fields={7: ("top", MARKER, "person")},
                                          accounts={"top": "person@example.com"}))
    open_step(monkeypatch)
    await settled_submit(THREAD, KEY, "ws://page")
    lifecycle._steps.pop(THREAD)
    await settled_submit(THREAD, KEY, "ws://page")
    assert holder.calls == []
    assert {"Runtime.callFunctionOn", "Runtime.evaluate"}.isdisjoint(method for method, _params in page.calls)


# Rule 4: the sentinel ------------------------------------------------------------------------------------------


def _holds_marker(value: object) -> bool:
    return MARKER in repr(value)


@pytest.mark.asyncio
async def test_rule4_sentinel_reaches_offer_login_and_nowhere_else(monkeypatch, holder, caplog):
    caplog.set_level("DEBUG")
    emitted = []
    monkeypatch.setattr(lifecycle, "_step_row_state", lambda _node_id: None)

    async def settle(_thread_id, step):
        emitted.append(("settle", vars(step)))

    async def emit(action, thread_id):
        emitted.append(("emit", action, thread_id))

    monkeypatch.setattr(lifecycle, "_settle_step", settle)
    monkeypatch.setattr(lifecycle, "_emit", emit)
    monkeypatch.setattr(browser_steps.state, "broadcast_to_thread", lambda *args: emitted.append(("broadcast", args)))
    monkeypatch.setattr(browser_steps.browser_pane_state, "report", lambda *args: emitted.append(("report", args)))
    use_page(monkeypatch, FakePage(fields={7: ("top", MARKER, "person@example.com")}))
    step = open_step(monkeypatch)
    monkeypatch.setitem(lifecycle._sessions, THREAD, "session-1")

    await settled_submit(THREAD, KEY, "ws://page")
    await lifecycle.step_event(THREAD, lifecycle.Event("probe", probe(), session="session-1"))

    assert holder.calls == [("offer_login", KEY, SITE, "person@example.com", MARKER), ("confirm_offer", KEY)]
    assert MARKER not in caplog.text
    assert not _holds_marker(vars(step))
    assert not _holds_marker(emitted)
    for module in (capture, lifecycle):
        assert not [name for name, value in vars(module).items()
                    if not isinstance(value, (types.ModuleType, types.FunctionType, type)) and _holds_marker(value)]


@pytest.mark.asyncio
async def test_rule4_a_failing_holder_logs_fixed_text_only(monkeypatch, holder, caplog):
    caplog.set_level("DEBUG")

    def refuse(*args):
        raise ValueError(f"refused {args}")

    monkeypatch.setattr(saved_login_offers, "offer_login", refuse)
    use_page(monkeypatch, FakePage(fields={7: ("top", MARKER, "person@example.com")}))
    open_step(monkeypatch)
    await settled_submit(THREAD, KEY, "ws://page")
    assert "saved-login capture skipped a submit it couldn't read" in caplog.text
    assert MARKER not in caplog.text
    assert capture._pending == {}


@pytest.mark.asyncio
async def test_offer_login_runs_off_the_event_loop(monkeypatch, holder):
    use_page(monkeypatch, FakePage(fields={7: ("top", MARKER, "person@example.com")}))
    open_step(monkeypatch)
    await settled_submit(THREAD, KEY, "ws://page")
    assert holder.names() == ["offer_login"]
    assert holder.offer_threads and threading.get_ident() not in holder.offer_threads


@pytest.mark.parametrize("change", ["ended", "swapped"])
@pytest.mark.asyncio
async def test_a_step_that_ends_while_the_holder_checks_drops_the_offer(monkeypatch, holder, change):
    recorded = saved_login_offers.offer_login

    def offer_then_step_changes(*args):
        lifecycle._steps.pop(THREAD)
        if change == "swapped":
            open_step(monkeypatch, visit="doc-9")
        return recorded(*args)

    monkeypatch.setattr(saved_login_offers, "offer_login", offer_then_step_changes)
    use_page(monkeypatch, FakePage(fields={7: ("top", MARKER, "person@example.com")}))
    open_step(monkeypatch)
    await settled_submit(THREAD, KEY, "ws://page")
    assert capture._pending == {}
    assert holder.calls == [("offer_login", KEY, SITE, "person@example.com", MARKER), ("drop_offer", KEY)]
    open_step(monkeypatch, visit="doc-10")
    capture.step_decided(THREAD, probe(), "settle", None)
    assert holder.names() == ["offer_login", "drop_offer"]


@pytest.mark.asyncio
async def test_an_offer_the_holder_didnt_make_is_never_confirmed_or_dropped(monkeypatch, holder):
    holder.offered = False
    use_page(monkeypatch, FakePage(fields={7: ("top", MARKER, "person@example.com")}))
    open_step(monkeypatch)
    await settled_submit(THREAD, KEY, "ws://page")
    assert capture._pending == {}
    capture.step_decided(THREAD, probe(), "settle", None)
    capture.session_ended(THREAD)
    assert holder.names() == ["offer_login"]


@pytest.mark.asyncio
async def test_a_page_that_never_answers_holds_the_submit_only_until_the_timeout(monkeypatch, holder):
    monkeypatch.setattr(capture, "CAPTURE_TIMEOUT", 0.05)
    page = use_page(monkeypatch, FakePage(fields={7: ("top", MARKER, "person")}))
    page.hang = True
    open_step(monkeypatch)
    await asyncio.wait_for(capture.person_submit(THREAD, KEY, "ws://page"), 1)
    assert holder.calls == []


@pytest.mark.asyncio
async def test_a_step_that_ended_during_the_read_gets_no_offer(monkeypatch, holder):
    page = use_page(monkeypatch, FakePage(fields={7: ("top", MARKER, "person")}))
    open_step(monkeypatch)
    original = FakeCdp.call

    async def end_step_mid_read(self, method, **params):
        if method == "Runtime.callFunctionOn":
            lifecycle._steps.pop(THREAD, None)
        return await original(self, method, **params)

    monkeypatch.setattr(FakeCdp, "call", end_step_mid_read)
    await settled_submit(THREAD, KEY, "ws://page")
    assert holder.calls == [] and page.calls


# Rule 5 -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("end", "visible", "opened", "settled"), [
    ("settle", frozenset(), None, "confirm_offer"),
    ("settle", frozenset({7}), None, "drop_offer"),
    ("settle", frozenset(), "password", "drop_offer"),
    ("done", frozenset(), None, "drop_offer"),
    ("declined", frozenset(), None, "drop_offer"),
    ("unsettled", frozenset(), None, "drop_offer"),
    (None, frozenset(), None, None),
])
def test_rule5_only_a_settled_step_with_its_field_gone_confirms(holder, end, visible, opened, settled):
    capture._pending[THREAD] = capture._Pending(KEY, "example.com")
    capture.step_decided(THREAD, probe(visible=visible), end, opened)
    assert holder.calls == ([(settled, KEY)] if settled else [])
    assert (THREAD in capture._pending) is (settled is None)


def test_rule5_a_session_end_drops_the_offer(holder):
    capture._pending[THREAD] = capture._Pending(KEY, "example.com")
    capture.step_decided(THREAD, None, "unsettled", None)
    capture._pending[THREAD] = capture._Pending(KEY, "example.com")
    capture.session_ended(THREAD)
    assert holder.calls == [("drop_offer", KEY), ("drop_offer", KEY)]
    assert capture._pending == {}


@pytest.mark.asyncio
async def test_rule5_a_failed_submit_that_comes_back_with_a_field_drops_through_the_lifecycle(monkeypatch, holder):
    monkeypatch.setattr(lifecycle, "_step_row_state", lambda _node_id: None)
    opened = []

    async def no_plan(*args):
        opened.append(args[0])

    async def open_new(thread_id, step):
        lifecycle._steps[thread_id] = step

    monkeypatch.setattr(lifecycle, "_settle_step", no_plan)
    monkeypatch.setattr(lifecycle, "_open_step", open_new)
    monkeypatch.setattr(browser_steps, "publish_password_page", lambda *_args: None)
    use_page(monkeypatch, FakePage(fields={7: ("top", MARKER, "person")}))
    open_step(monkeypatch)
    monkeypatch.setitem(lifecycle._sessions, THREAD, "session-1")
    await settled_submit(THREAD, KEY, "ws://page")
    await lifecycle.step_event(THREAD, lifecycle.Event("probe", probe(visible=frozenset({11})), session="session-1"))
    assert holder.names() == ["offer_login", "drop_offer"]
    assert isinstance(lifecycle._steps.get(THREAD), browser_steps.PasswordStep)


# Rule 6 -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("host", "dropped"), [("other.example.org", True), ("www.example.com", False), ("", True)])
def test_rule6_a_probe_of_another_registrable_domain_drops_the_offer(holder, host, dropped):
    capture._pending[THREAD] = capture._Pending(KEY, "example.com")
    capture.step_decided(THREAD, probe(host=host, visible=frozenset({7})), None, None)
    assert holder.calls == ([("drop_offer", KEY)] if dropped else [])


# Rule 7 -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("args", "writes"), [
    (("fill", "@e1", "x"), True), (("type", "@e1", "x"), True), (("press", "Enter"), True),
    (("select", "@e1", "a"), True), (("eval", "1"), True), (("upload", "@e1", "/tmp/f"), True),
    (("paste",), True), (("insert-text", "x"), True), (("keyboard", "type", "x"), True),
    (("find", "label", "Email", "fill", "x"), True), (("wait", "--fn", "x"), True),
    (("batch", "snapshot", "fill @e1 x"), True), (("batch", "--bail", "click @e1", "get url"), False),
    (("click", "@e1"), False), (("dblclick", "@e1"), False), (("hover", "@e1"), False), (("scroll", "down"), False),
    (("scrollintoview", "@e1"), False), (("get", "url"), False), (("snapshot", "-i"), False),
    (("open", "https://example.com"), False), (("back",), False), (("reload",), False), (("wait", "500"), False),
])
def test_rule7_which_agent_commands_mark_the_document(args, writes):
    assert capture.writes_value(args) is writes


@pytest.mark.asyncio
async def test_rule7_an_agent_mark_on_the_document_stops_offers_and_username_notes(monkeypatch, holder):
    page = use_page(monkeypatch, FakePage(fields={7: ("top", MARKER, "person")}, accounts={"top": "person@example.com"}))
    await capture.agent_writing(THREAD, "ws://page")
    open_step(monkeypatch)
    await settled_submit(THREAD, KEY, "ws://page")
    lifecycle._steps.pop(THREAD)
    await settled_submit(THREAD, KEY, "ws://page")
    assert holder.calls == []
    assert {"Runtime.callFunctionOn", "Runtime.evaluate"}.isdisjoint(method for method, _params in page.calls)
    # A new document is unmarked.
    page.loader = "doc-2"
    open_step(monkeypatch)
    await settled_submit(THREAD, KEY, "ws://page")
    assert holder.names() == ["offer_login"]


@pytest.mark.asyncio
async def test_rule7_session_end_clears_marks_and_an_unreadable_page_marks_every_document(monkeypatch, holder):
    page = use_page(monkeypatch, FakePage(fields={7: ("top", MARKER, "person")}))
    await capture.agent_writing(THREAD, "ws://page")
    capture.session_ended(THREAD)
    open_step(monkeypatch)
    await settled_submit(THREAD, KEY, "ws://page")
    assert holder.names() == ["offer_login"]
    holder.calls.clear()
    await capture.agent_writing(THREAD, "")
    page.loader = "doc-9"
    await settled_submit(THREAD, KEY, "ws://page")
    assert holder.calls == [("drop_offer", KEY)]


@pytest.mark.asyncio
@pytest.mark.parametrize(("args", "marked"), [(("fill", "@e1", "x"), True), (("click", "@e1"), False)])
async def test_rule7_the_agent_command_path_marks_before_a_writing_command_runs(monkeypatch, args, marked):
    order = []

    async def page_endpoint(_command):
        return "ws://page"

    async def check_page_access(*_args, **_kwargs):
        return PageAccess(None, False)

    async def agent_writing(thread_id, endpoint):
        order.append(("mark", thread_id, endpoint))

    async def run_admitted(_runtime, ran, **_kwargs):
        order.append(("run", ran[0]))
        return CommandResult(0, "{}", ""), False

    async def finished(*_args):
        return None

    async def not_dropdown(*_args):
        return False

    monkeypatch.setattr(browser_session_commands.browser_cdp, "page_endpoint", page_endpoint)
    monkeypatch.setattr(browser_session_commands.browser_control, "check_page_access", check_page_access)
    monkeypatch.setattr(browser_session_commands.browser_dom_write, "is_dropdown", not_dropdown)
    monkeypatch.setattr(browser_session_commands.browser_pane_state, "has_pane", lambda _thread: False)
    monkeypatch.setattr(browser_session_commands.browser_pane_state, "page_command_finished", finished)
    monkeypatch.setattr(capture, "agent_writing", agent_writing)
    owner = SimpleNamespace(_run_admitted=run_admitted, _model_result=lambda _result, _record: "ok",
                            session_identity=lambda _thread: "session-1")
    runtime = SimpleNamespace(record=SimpleNamespace(binding="bound", session="session-1", profile=None, copy_of=None,
                                                     shared=False))
    assert await browser_session_commands._run_action(owner, runtime, THREAD, args, None, 10, False) == "ok"
    assert order == ([("mark", THREAD, "ws://page")] if marked else []) + [("run", args[0])]


# Rule 8 -----------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rule8_a_filled_username_is_noted_on_a_page_with_no_password_step(monkeypatch, holder):
    use_page(monkeypatch, FakePage(frames=(("top", "https://shell.example.com"), ("child", SITE)),
                                   accounts={"child": "person@example.com"}))
    await settled_submit(THREAD, KEY, "ws://page")
    assert holder.calls == [("note_username", KEY, SITE, "person@example.com")]


@pytest.mark.asyncio
async def test_rule8_no_username_or_an_open_password_step_notes_nothing(monkeypatch, holder):
    page = use_page(monkeypatch, FakePage(accounts={}))
    await settled_submit(THREAD, KEY, "ws://page")
    assert holder.calls == []
    page.accounts = {"top": "person@example.com"}
    page.fields = {7: ("top", "", "person@example.com")}
    open_step(monkeypatch)
    await settled_submit(THREAD, KEY, "ws://page")
    assert holder.calls == []


@pytest.mark.asyncio
async def test_two_inflight_submits_keep_the_real_holders_noted_username(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = saved_login_offers._checked_offer
    lock = threading.Lock()
    calls = 0
    active = 0

    def checked(*args):
        nonlocal calls, active
        with lock:
            calls += 1
            ordinal = calls
            active += 1
            assert active == 1, "holder calls overlapped"
        try:
            if ordinal == 1:
                entered.set()
                assert release.wait(3)
            return original(*args)
        finally:
            with lock:
                active -= 1

    saved_login_offers.drop_thread(THREAD)
    monkeypatch.setattr(saved_login_offers, "is_own_site", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(saved_login_offers, "logins_for_site", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(saved_login_offers, "_checked_offer", checked)
    saved_login_offers.note_username(KEY, SITE, "person@example.com")
    page = use_page(monkeypatch, FakePage(fields={7: ("top", MARKER, "")}))
    open_step(monkeypatch)
    try:
        first = asyncio.create_task(capture.person_submit(THREAD, KEY, "ws://page"))
        assert await asyncio.to_thread(entered.wait, 2)
        assert first.done(), "input waited for the holder"
        page.fields[7] = ("top", "new-value", "")
        await capture.person_submit(THREAD, KEY, "ws://page")
        release.set()
        await first
        await drain_capture()
        capture.step_decided(THREAD, probe(), "settle", None)
        await drain_capture()
        offer = saved_login_offers.pending_offer(KEY)
        assert offer is not None and offer.username == "person@example.com"
        assert saved_login_offers._offers[KEY] == saved_login_offers._Offer(
            SITE, "person@example.com", "new-value", update=False, confirmed=True)
        assert calls == 2
    finally:
        release.set()
        saved_login_offers.drop_thread(THREAD)


@pytest.mark.asyncio
async def test_empty_first_submit_keeps_the_real_holders_noted_username(monkeypatch):
    saved_login_offers.drop_thread(THREAD)
    monkeypatch.setattr(saved_login_offers, "is_own_site", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(saved_login_offers, "logins_for_site", lambda *_args, **_kwargs: [])
    saved_login_offers.note_username(KEY, SITE, "person@example.com")
    page = use_page(monkeypatch, FakePage(fields={7: ("top", "", "")}))
    open_step(monkeypatch)
    try:
        await settled_submit(THREAD, KEY, "ws://page")
        page.fields[7] = ("top", MARKER, "")
        await settled_submit(THREAD, KEY, "ws://page")
        capture.step_decided(THREAD, probe(), "settle", None)
        offer = saved_login_offers.pending_offer(KEY)
        assert offer is not None and offer.username == "person@example.com"
    finally:
        saved_login_offers.drop_thread(THREAD)


@pytest.mark.asyncio
@pytest.mark.parametrize("later", ["different", "marked", "timeout"])
async def test_later_non_login_submit_drops_earlier_offer(monkeypatch, holder, later):
    page = use_page(monkeypatch, FakePage(fields={7: ("top", MARKER, "person")}))
    open_step(monkeypatch, nodes=frozenset({7, 8}))
    await settled_submit(THREAD, KEY, "ws://page")
    if later == "different":
        page.fields[8] = ("top", "another-value", "person")
    elif later == "marked":
        await capture.agent_writing(THREAD, "ws://page")
    else:
        monkeypatch.setattr(capture, "CAPTURE_TIMEOUT", 0.01)
        page.hang = True
    await settled_submit(THREAD, KEY, "ws://page")
    capture.step_decided(THREAD, probe(), "settle", None)
    assert holder.names() == ["offer_login", "drop_offer"]
    assert THREAD not in capture._pending


@pytest.mark.asyncio
@pytest.mark.parametrize(("event", "expected"), [("open", None), ("settle", "confirm_offer"),
                                                ("domain", "drop_offer"), ("session", "drop_offer")])
async def test_step_and_session_events_while_holder_runs(monkeypatch, holder, event, expected):
    entered, release = threading.Event(), threading.Event()
    offer = saved_login_offers.offer_login

    def slow(*args):
        entered.set()
        assert release.wait(3)
        return offer(*args)

    monkeypatch.setattr(saved_login_offers, "offer_login", slow)
    monkeypatch.setattr(capture, "CAPTURE_TIMEOUT", 0.05)
    use_page(monkeypatch, FakePage(fields={7: ("top", MARKER, "person")}))
    open_step(monkeypatch)
    try:
        await asyncio.wait_for(capture.person_submit(THREAD, KEY, "ws://page"), 0.05)
        assert await asyncio.to_thread(entered.wait, 2)
        if event == "settle":
            capture.step_decided(THREAD, probe(), "settle", None)
        elif event == "domain":
            capture.step_decided(THREAD, probe(host="other.example.org"), None, None)
        elif event == "session":
            capture.session_ended(THREAD)
        release.set()
        await drain_capture()
        assert holder.names() == (["offer_login", expected] if expected else ["offer_login"])
        assert (THREAD in capture._pending) is (event == "open")
    finally:
        release.set()
        await drain_capture()


@pytest.mark.asyncio
async def test_out_of_order_reads_still_call_holder_in_submit_order(monkeypatch, holder):
    entered, release = asyncio.Event(), asyncio.Event()
    count = 0

    async def read(_thread, _endpoint, _step):
        nonlocal count
        count += 1
        if count == 1:
            entered.set()
            await release.wait()
            return SITE, "person", "first-value"
        return SITE, "person", "second-value"

    monkeypatch.setattr(capture, "_submit", read)
    open_step(monkeypatch)
    first = asyncio.create_task(capture.person_submit(THREAD, KEY, "ws://page"))
    await entered.wait()
    await capture.person_submit(THREAD, KEY, "ws://page")
    assert holder.calls == []
    release.set()
    await first
    await drain_capture()
    assert [call[-1] for call in holder.calls] == ["first-value", "second-value"]
    assert THREAD in capture._pending


@pytest.mark.asyncio
async def test_failed_step_drops_prior_offer_when_latest_queued_call_is_skipped(monkeypatch, holder):
    entered, release = threading.Event(), threading.Event()
    offer = saved_login_offers.offer_login

    def slow(*args):
        entered.set()
        assert release.wait(3)
        return offer(*args)

    monkeypatch.setattr(saved_login_offers, "offer_login", slow)
    use_page(monkeypatch, FakePage(fields={7: ("top", MARKER, "person")}))
    open_step(monkeypatch)
    try:
        await capture.person_submit(THREAD, KEY, "ws://page")
        assert await asyncio.to_thread(entered.wait, 2)
        await capture.person_submit(THREAD, KEY, "ws://page")
        capture.step_decided(THREAD, probe(visible=frozenset({7})), "unsettled", None)
        release.set()
        await drain_capture()
        assert holder.names() == ["offer_login", "drop_offer"]
        assert THREAD not in capture._pending
    finally:
        release.set()
        await drain_capture()


@pytest.mark.asyncio
async def test_any_document_mark_is_never_evicted(monkeypatch):
    page = use_page(monkeypatch, FakePage())
    await capture.agent_writing(THREAD, "")
    for number in range(100):
        page.loader = f"document-{number}"
        await capture.agent_writing(THREAD, "ws://page")
    assert capture._marked(THREAD, "another-document")
    assert capture._ANY_DOCUMENT in capture._marks[THREAD]


@pytest.mark.asyncio
async def test_session_end_skips_queued_old_session_offer_before_new_session(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = saved_login_offers._checked_offer
    checks = []

    def checked(*args):
        checks.append(args[1])
        if len(checks) == 1:
            entered.set()
            assert release.wait(3)
        return original(*args)

    saved_login_offers.drop_thread(THREAD)
    monkeypatch.setattr(saved_login_offers, "is_own_site", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(saved_login_offers, "logins_for_site", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(saved_login_offers, "_checked_offer", checked)
    use_page(monkeypatch, FakePage(fields={7: ("top", MARKER, "person@example.com")}))
    open_step(monkeypatch)
    new_key = f"{THREAD}:session-2"
    try:
        await capture.person_submit(THREAD, KEY, "ws://page")
        assert await asyncio.to_thread(entered.wait, 2)
        await capture.person_submit(THREAD, KEY, "ws://page")
        saved_login_offers.drop_thread(THREAD)
        capture.session_ended(THREAD)
        open_step(monkeypatch, visit="doc-new")
        await capture.person_submit(THREAD, new_key, "ws://page")
        release.set()
        await drain_capture()
        capture.step_decided(THREAD, probe(), "settle", None)
        assert KEY not in saved_login_offers._offers
        assert saved_login_offers.pending_offer(KEY) is None
        assert saved_login_offers.pending_offer(new_key) is not None
        assert len(checks) == 2
    finally:
        release.set()
        await drain_capture()
        saved_login_offers.drop_thread(THREAD)


@pytest.mark.asyncio
@pytest.mark.parametrize("event", ["step", "session"])
async def test_real_holder_has_no_offer_after_slow_call_and_ending(monkeypatch, event):
    entered, release = threading.Event(), threading.Event()
    original = saved_login_offers._checked_offer

    def checked(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)

    saved_login_offers.drop_thread(THREAD)
    monkeypatch.setattr(saved_login_offers, "is_own_site", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(saved_login_offers, "logins_for_site", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(saved_login_offers, "_checked_offer", checked)
    monkeypatch.setattr(capture, "CAPTURE_TIMEOUT", 0.02)
    use_page(monkeypatch, FakePage(fields={7: ("top", MARKER, "person@example.com")}))
    open_step(monkeypatch)
    try:
        await capture.person_submit(THREAD, KEY, "ws://page")
        assert await asyncio.to_thread(entered.wait, 2)
        await asyncio.sleep(0.03)
        if event == "step":
            capture.step_decided(THREAD, probe(visible=frozenset({7})), "unsettled", None)
        else:
            saved_login_offers.drop_thread(THREAD)
            capture.session_ended(THREAD)
        release.set()
        await drain_capture()
        assert saved_login_offers.pending_offer(KEY) is None
        assert KEY not in saved_login_offers._offers
        assert THREAD not in capture._pending
    finally:
        release.set()
        await drain_capture()
        saved_login_offers.drop_thread(THREAD)


@pytest.mark.asyncio
async def test_ended_session_skips_queued_username_and_new_session_works(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = saved_login_offers._checked_offer

    def checked(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)

    saved_login_offers.drop_thread(THREAD)
    monkeypatch.setattr(saved_login_offers, "is_own_site", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(saved_login_offers, "logins_for_site", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(saved_login_offers, "_checked_offer", checked)
    use_page(monkeypatch, FakePage(fields={7: ("top", MARKER, "person@example.com")},
                                  accounts={"top": "person@example.com"}))
    open_step(monkeypatch)
    new_key = f"{THREAD}:session-2"
    try:
        await capture.person_submit(THREAD, KEY, "ws://page")
        assert await asyncio.to_thread(entered.wait, 2)
        monkeypatch.delitem(lifecycle._steps, THREAD)
        await capture.person_submit(THREAD, KEY, "ws://page")
        saved_login_offers.drop_thread(THREAD)
        capture.session_ended(THREAD, KEY)
        await capture.person_submit(THREAD, new_key, "ws://page")
        release.set()
        await drain_capture()
        assert KEY not in saved_login_offers._usernames
        assert KEY not in saved_login_offers._offers
        assert saved_login_offers._usernames[new_key] == (SITE, "person@example.com")
    finally:
        release.set()
        await drain_capture()
        saved_login_offers.drop_thread(THREAD)
