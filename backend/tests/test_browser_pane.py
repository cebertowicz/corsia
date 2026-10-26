import asyncio
import contextlib
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, WebSocketDisconnect

from api import browser
from host_access import browser_cdp, browser_cli, browser_control, browser_pane, browser_pane_state
from host_access.browser_cli import CommandResult
from host_access.browser_session_state import BrowserRuntime, BrowserSessionError, BrowserSessionRecord
from host_access.browser_sessions import BrowserSessionOwner
from tools import browser_runtime
from ws import browser_stream

# The real screencast owner, before the autouse fixture replaces it.
_SCREENCAST = browser_pane.screencast


@pytest.fixture(autouse=True)
def _no_browser_guard_state():
    browser_pane_state._panes.clear()
    browser_pane_state._pane_probes.clear()
    browser_control._steps.clear()
    browser_control._guarded_visits.clear()
    browser_control._probe_locks.clear()
    browser_pane_state._screencasts.clear()
    browser_pane_state._capture_sizes.clear()
    browser_pane_state._agent_seen.clear()
    yield
    browser_pane_state._panes.clear()
    browser_pane_state._pane_probes.clear()
    browser_control._steps.clear()
    browser_control._guarded_visits.clear()
    browser_control._probe_locks.clear()
    browser_pane_state._screencasts.clear()
    browser_pane_state._capture_sizes.clear()
    browser_pane_state._agent_seen.clear()


class FakePane:
    def __init__(self, messages=()):
        self.messages = list(messages)
        self.json = []
        self.text = []
        self.closed = []
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def receive_json(self):
        return {"token": "valid"}

    async def receive_text(self):
        if self.messages:
            return self.messages.pop(0)
        raise WebSocketDisconnect()

    async def send_json(self, data):
        self.json.append(data)

    async def send_text(self, data):
        self.text.append(data)

    async def close(self, code=None):
        self.closed.append(code)


class IdleScreencast:
    """Stands in for a pane's page screencast, recording which panes it served and when each ended."""

    def __init__(self, frames=()):
        self.frames = list(frames)
        self.started = []
        self.stopped = []

    async def __call__(self, thread_id, connection_token, deliver):
        self.started.append((thread_id, connection_token))
        try:
            for frame in self.frames:
                deliver(json.dumps(frame))
            await asyncio.Future()
        finally:
            self.stopped.append((thread_id, connection_token))


@pytest.fixture(autouse=True)
def screencast(monkeypatch):
    cast = IdleScreencast()
    monkeypatch.setattr(browser_pane, "screencast", cast)
    return cast


def test_open_url_accepts_http_and_https_only():
    assert browser._http_url("https://example.test/path") == "https://example.test/path"
    assert browser._http_url("http://example.test") == "http://example.test"
    for url in ("file:///tmp/x", "javascript:alert(1)", "https:"):
        with pytest.raises(HTTPException) as error:
            browser._http_url(url)
        assert error.value.status_code == 422


def test_open_rejects_missing_auth():
    with pytest.raises(HTTPException) as error:
        browser._owned_thread("thread-1")
    assert error.value.status_code == 401


def test_open_rejects_foreign_thread(monkeypatch):
    monkeypatch.setattr(browser, "authenticate", lambda _token: {"id": "user-1"})
    monkeypatch.setattr(browser, "get_thread_for_user", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(browser, "get_db", lambda: SimpleNamespace(close=lambda: None))
    with pytest.raises(HTTPException) as error:
        browser._owned_thread("thread-1", "Bearer valid")
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_stream_closes_for_foreign_thread(monkeypatch):
    pane = FakePane()
    monkeypatch.setattr(browser_stream, "authenticate", lambda _token: {"id": "user-1"})
    monkeypatch.setattr(browser_stream, "_user_owns_thread", lambda *_args: False)
    await browser_stream.browser_stream_endpoint(pane, "foreign-thread")
    assert pane.accepted
    assert pane.closed == [1008]


@pytest.mark.asyncio
async def test_stream_does_not_close_a_pane_that_left_before_auth():
    class LeavingPane(FakePane):
        async def receive_json(self):
            raise WebSocketDisconnect()

    pane = LeavingPane()
    await browser_stream.browser_stream_endpoint(pane, "thread-1")
    assert pane.closed == []


def _rendered(data: dict) -> str:
    """The string BrowserSessionOwner.run returns for one successful agent-browser --json command."""
    return browser_cli.render_model_result(CommandResult(0, json.dumps({"success": True, "data": data}), ""), redactions={})


def _raw(data: dict) -> CommandResult:
    """One successful agent-browser --json result, as the session's own commands receive it."""
    return CommandResult(0, json.dumps({"success": True, "data": data}), "")


class RenderedOwner:
    """Resolves the pane's page target through the agent commands' resolver, as the session owner does."""

    def __init__(self):
        self.commands = []

    async def page_endpoint(self, _thread_id):
        async def run(args, _timeout):
            self.commands.append(tuple(args))
            if tuple(args) == ("get", "cdp-url"):
                return _raw({"cdpUrl": "ws://browser.test/devtools/browser/1"})
            assert tuple(args) == ("get", "url")
            return _raw({"url": "https://example.test/current"})

        return await browser_cdp.page_endpoint(run)


@pytest.mark.asyncio
async def test_relay_delivers_click_shortcut_text_then_enter_in_order_on_one_cdp_target(monkeypatch, screencast):
    click = [{"type": "input_mouse", "eventType": kind, "x": 5, "y": 6, "button": "left", "clickCount": 1}
             for kind in ("mousePressed", "mouseReleased")]
    keys = [
        {"type": "input_keyboard", "eventType": "keyDown", "key": "a", "code": "KeyA", "keyCode": 65, "modifiers": 4},
        {"type": "input_keyboard", "eventType": "keyUp", "key": "a", "code": "KeyA", "keyCode": 65, "modifiers": 4},
        {"type": "input_keyboard", "eventType": "keyDown", "key": "n", "code": "KeyN", "keyCode": 78, "text": "n", "modifiers": 0},
        {"type": "input_keyboard", "eventType": "keyUp", "key": "n", "code": "KeyN", "keyCode": 78, "modifiers": 0},
        {"type": "input_keyboard", "eventType": "keyDown", "key": "Enter", "code": "Enter", "keyCode": 13, "text": "\r", "modifiers": 0},
        {"type": "input_keyboard", "eventType": "keyUp", "key": "Enter", "code": "Enter", "keyCode": 13, "modifiers": 0},
    ]
    pane = FakePane(json.dumps(event) for event in (*click, *keys))
    delivered = []

    def recorder(kind):
        async def record(endpoint, event):
            delivered.append((kind, endpoint, event.get("eventType"), event.get("key")))
        return record

    async def no_metadata(_endpoint, _report):
        await asyncio.Future()

    owner = RenderedOwner()
    monkeypatch.setattr(browser_pane.PROCESS_OWNER, "browser_sessions", lambda: owner)
    monkeypatch.setattr(browser_pane.browser_cdp, "_page_endpoint", lambda _endpoint, _expected_url: "ws://page.test/current")
    monkeypatch.setattr(browser_pane.browser_cdp, "dispatch_mouse_event", recorder("mouse"))
    monkeypatch.setattr(browser_pane.browser_cdp, "dispatch_key_event", recorder("key"))
    monkeypatch.setattr(browser_pane.browser_cdp, "watch_page_metadata", no_metadata)
    connected, inputs = [], []
    pane_connected = browser_control.pane_connected
    monkeypatch.setattr(browser_control, "pane_connected", lambda *args: connected.append(args) or pane_connected(*args))
    monkeypatch.setattr(browser_control, "person_input", inputs.append)
    with pytest.raises(WebSocketDisconnect):
        await browser_stream._relay(pane, "thread-1", "user-1")
    # Every person event reached the page target in the order typed, so nothing could overtake
    # the shortcut, the text or the submit.
    assert len(screencast.started) == 1
    # Each one is noted for an open password step's re-probe, which uses this pane's own probe.
    assert inputs == ["thread-1"] * len(click + keys)
    [(_thread, token, _report, probe)] = connected
    assert probe.func is browser_pane.password_probe and probe.args == ("thread-1", token)
    assert delivered == [
        ("mouse", "ws://page.test/current", "mousePressed", None),
        ("mouse", "ws://page.test/current", "mouseReleased", None),
        *(("key", "ws://page.test/current", event["eventType"], event["key"]) for event in keys),
    ]
    assert not browser_pane_state.has_pane("thread-1")


@pytest.mark.asyncio
async def test_a_pane_resolves_its_page_target_once_through_the_agent_commands_resolver(monkeypatch):
    calls = []
    owner = RenderedOwner()
    monkeypatch.setattr(browser_pane.PROCESS_OWNER, "browser_sessions", lambda: owner)
    monkeypatch.setattr(browser_pane.browser_cdp, "_page_endpoint", lambda endpoint, expected_url: calls.append((endpoint, expected_url)) or "ws://page.test/current")
    browser_pane.clear_page_endpoint("thread-1", "pane-1")
    try:
        assert await browser_pane._page_endpoint("thread-1", "pane-1", "failed") == "ws://page.test/current"
        assert await browser_pane._page_endpoint("thread-1", "pane-1", "failed") == "ws://page.test/current"
        assert owner.commands == [("get", "cdp-url"), ("get", "url")]
        assert calls == [("ws://browser.test/devtools/browser/1", "https://example.test/current")]
    finally:
        browser_pane.clear_page_endpoint("thread-1", "pane-1")


@pytest.mark.asyncio
async def test_a_pane_leaving_mid_read_lets_the_running_command_finish(monkeypatch):
    # Cancelling a running command resets the shared session, so a pane that leaves while its endpoint
    # read runs must not cancel that read.
    started, release, finished = asyncio.Event(), asyncio.Event(), []

    class SlowOwner:
        async def page_endpoint(self, _thread_id):
            started.set()
            await release.wait()
            finished.append(("get", "cdp-url"))
            return "ws://page.test/current"

    monkeypatch.setattr(browser_pane.PROCESS_OWNER, "browser_sessions", lambda: SlowOwner())
    browser_pane.clear_page_endpoint("thread-1", "pane-1")
    relay = asyncio.create_task(browser_pane._page_endpoint("thread-1", "pane-1", "failed"))
    await started.wait()
    relay.cancel()
    with pytest.raises(asyncio.CancelledError):
        await relay
    release.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert finished == [("get", "cdp-url")]
    browser_pane.clear_page_endpoint("thread-1", "pane-1")


@pytest.mark.asyncio
async def test_page_info_reads_url_and_title_from_the_session_owners_rendered_result(monkeypatch):
    class InfoOwner:
        async def run(self, _thread_id, args, **_kwargs):
            return {("get", "url"): _rendered({"url": "https://www.apple.com/"}),
                    ("get", "title"): _rendered({"title": "Apple"})}[tuple(args)]

    monkeypatch.setattr(browser_pane.PROCESS_OWNER, "browser_sessions", lambda: InfoOwner())
    # The pane's address and tab label show these values, never the rendered JSON around them.
    assert await browser_pane.page_info("thread-1") == ("https://www.apple.com/", "Apple")


def test_pane_presence_is_not_exclusive_control():
    browser_control.pane_connected("thread-1", "first")
    browser_control.pane_connected("thread-1", "second")
    assert browser_pane_state.has_pane("thread-1")
    browser_control.pane_disconnected("thread-1", "first")
    assert browser_pane_state.has_pane("thread-1")
    browser_control.pane_disconnected("thread-1", "second")
    assert not browser_pane_state.has_pane("thread-1")


@pytest.mark.asyncio
async def test_shared_pane_does_not_refuse_agent_commands_for_presence(monkeypatch):
    browser_control.pane_connected("thread-1", "pane-1")
    assert browser_pane_state.has_pane("thread-1")
    owner = BrowserSessionOwner(namespace="corsia-pane-test")

    def past_the_gate(_args):
        raise RuntimeError("past the control gate")

    monkeypatch.setattr(browser_runtime.browser_cli, "check_local_mode", past_the_gate)
    with pytest.raises(RuntimeError, match="past the control gate"):
        await owner.run("thread-1", ("open", "https://example.test"), pane=True)


@pytest.mark.asyncio
async def test_streamless_pane_open_only_starts_a_new_session():
    owner = BrowserSessionOwner(namespace="corsia-pane-test")
    owner._sessions["thread-1"] = BrowserRuntime(BrowserSessionRecord("thread-1", "test-session"))
    with pytest.raises(BrowserSessionError, match="already open"):
        await owner._admit("thread-1", lease_id=None, session="new")


@pytest.mark.asyncio
async def test_pane_stream_commands_never_start_a_session():
    owner = BrowserSessionOwner(namespace="corsia-pane-test")
    with pytest.raises(BrowserSessionError, match="no longer open"):
        await owner._admit("thread-1", lease_id=None, session="existing")
    assert owner.live_count() == 0


@pytest.mark.parametrize(("data", "parsed"), [
    ({"width": 680, "height": 1030, "scale": 2, "scheme": "dark"}, (680, 1030, 2.0, "dark")),
    ({"width": 680.6, "height": 1030, "scale": 1.25, "scheme": "light"}, (680, 1030, 1.25, "light")),
    ({"width": 120, "height": 1030, "scale": 2, "scheme": "dark"}, None),
    ({"width": 680, "height": 1030, "scale": 0, "scheme": "dark"}, None),
    ({"width": True, "height": 1030, "scale": 2, "scheme": "dark"}, None),
    ({"width": "680", "height": 1030, "scale": 2, "scheme": "dark"}, None),
    ({"width": 680, "height": 1030, "scale": 2, "scheme": "auto"}, None),
])
def test_pane_appearance_accepts_only_screen_sized_themed_panes(data, parsed):
    appearance = browser_pane.PaneAppearance.parse(data)
    assert (appearance and (appearance.width, appearance.height, appearance.scale, appearance.scheme)) == parsed


@pytest.mark.asyncio
async def test_match_pane_sets_size_density_and_theme_on_the_open_session(monkeypatch):
    calls = []

    class Owner:
        async def run(self, thread_id, args, **kwargs):
            calls.append((args, kwargs))
            return "ok"

    monkeypatch.setattr(browser_pane.PROCESS_OWNER, "browser_sessions", lambda: Owner())
    await browser_pane.match_pane("thread-1", browser_pane.PaneAppearance(680, 1030, 2.0, "dark"))
    assert calls == [
        (("set", "viewport", "680", "1030", "2"), {"pane": True, "session": "existing"}),
        (("set", "media", "dark"), {"pane": True, "session": "existing"}),
    ]


@pytest.mark.asyncio
async def test_pane_resizes_are_matched_one_at_a_time_latest_first(monkeypatch):
    fitted, release = [], asyncio.Event()

    async def match_pane(thread_id, appearance):
        fitted.append((appearance.width, appearance.height))
        await release.wait()

    monkeypatch.setattr(browser_stream.browser_pane, "match_pane", match_pane)
    sizes = [{"type": "appearance", "width": w, "height": 600, "scale": 2, "scheme": "dark"} for w in (400, 500, 600)]
    pane = SimpleNamespace(sent=[], messages=[json.dumps(size) for size in sizes])

    async def receive_text():
        await asyncio.sleep(0)
        if pane.messages:
            return pane.messages.pop(0)
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.sleep(0.05)
        raise browser_stream.WebSocketDisconnect()

    async def send_json(payload):
        pane.sent.append(payload)

    async def send_text(payload):
        pane.sent.append(json.loads(payload))

    pane.receive_text, pane.send_json, pane.send_text = receive_text, send_json, send_text
    with pytest.raises(browser_stream.WebSocketDisconnect):
        await browser_stream._relay(pane, "thread-1", "user-1")
    # The first fit runs; the two that arrive meanwhile collapse into the latest one.
    assert fitted == [(400, 600), (600, 600)]


@pytest.mark.asyncio
async def test_a_newer_pane_size_supersedes_an_older_connections(monkeypatch):
    fitted, release = [], asyncio.Event()

    async def match_pane(thread_id, appearance):
        fitted.append(appearance.height)
        await release.wait()

    monkeypatch.setattr(browser_stream.browser_pane, "match_pane", match_pane)
    size = {"width": 400, "scale": 1, "scheme": "light"}
    browser_stream._request_match("thread-1", "old-pane", {**size, "height": 1030})
    await asyncio.sleep(0)
    browser_stream._request_match("thread-1", "old-pane", {**size, "height": 1000})
    browser_stream._request_match("thread-1", "new-pane", {**size, "height": 830})
    release.set()
    await browser_stream._runners["thread-1"]
    assert fitted == [1030, 830]
    assert "thread-1" not in browser_stream._runners


@pytest.mark.asyncio
async def test_agent_close_remains_available_while_a_pane_is_connected(monkeypatch):
    closed = []
    async def close(thread_id):
        closed.append(thread_id)
        return "Browser session closed."
    monkeypatch.setattr(browser_runtime.PROCESS_OWNER, "close_browser", close)
    browser_control.pane_connected("thread-1", "pane-1")
    assert await browser_runtime.BrowserCloseTool().execute(_thread_id="thread-1") == "Browser session closed."
    assert closed == ["thread-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("returncode", "show_window", "announced"), [
    (0, False, ["thread-1", "thread-1"]),
    (0, True, ["thread-1", "thread-1"]),
    (1, True, []),
])
async def test_browser_opened_is_announced_after_every_successful_shared_open(monkeypatch, returncode, show_window, announced):
    owner = BrowserSessionOwner(namespace="corsia-pane-test")
    runtime = BrowserRuntime(BrowserSessionRecord("thread-1", "test-session", show_window=show_window))
    seen = []
    monkeypatch.setattr(browser_runtime.browser_cli, "check_local_mode", lambda _args: None)
    monkeypatch.setattr(owner, "_pin_native", lambda: asyncio.sleep(0))
    monkeypatch.setattr(owner, "_admit", lambda *_args, **_kwargs: asyncio.sleep(0, result=runtime))
    monkeypatch.setattr(owner, "_run_admitted", lambda *_args, **_kwargs: asyncio.sleep(
        0, result=(CommandResult(returncode, "", "failed"), False)))
    monkeypatch.setattr(browser_control, "announce_browser_opened", seen.append)
    await owner.run("thread-1", ("open", "https://example.test"), session="new")
    await owner.run("thread-1", ("open", "https://example.test"), session="new")
    assert seen == announced


@pytest.mark.asyncio
async def test_navigate_accepts_an_authenticated_shared_pane(monkeypatch):
    opened = []

    async def open_url(thread_id, url, token):
        opened.append((thread_id, url, token))
        return "ok"

    async def go(thread_id, action):
        opened.append((thread_id, action))
        return "ok"

    async def page_info(_thread_id):
        return ("https://www.example.test/path", "Example page")

    monkeypatch.setattr(browser_stream.browser_pane, "open_url", open_url)
    monkeypatch.setattr(browser_stream.browser_pane, "go", go)
    monkeypatch.setattr(browser_stream.browser_pane, "page_info", page_info)
    assert (await browser_stream._navigate("thread-1", "pane-1", {"url": "file:///etc/hosts"}))["error"]
    assert (await browser_stream._navigate("thread-1", "pane-1", {"action": "close"}))["error"] == "Unknown browser action"
    navigated = await browser_stream._navigate("thread-1", "pane-1", {"url": "https://example.test"})
    assert navigated == {"type": "navigate_result", "error": None, "url": "https://www.example.test/path",
                         "domain": "example.test", "title": "Example page"}
    for action in ("back", "forward", "reload"):
        assert (await browser_stream._navigate("thread-1", "pane-1", {"action": action}))["error"] is None
    assert opened == [("thread-1", "https://example.test", "pane-1"), ("thread-1", "back"),
                      ("thread-1", "forward"), ("thread-1", "reload")]


@pytest.mark.asyncio
async def test_has_session_reports_only_an_open_browser():
    owner = BrowserSessionOwner(namespace="corsia-pane-test")
    assert not await owner.has_session("thread-1")
    owner._sessions["thread-1"] = BrowserRuntime(BrowserSessionRecord("thread-1", "test-session"))
    assert await owner.has_session("thread-1")


def _authenticated_pane(monkeypatch, *, open_browser):
    monkeypatch.setattr(browser_stream, "authenticate", lambda _token: {"id": "user-1"})
    monkeypatch.setattr(browser_stream, "_user_owns_thread", lambda *_args: True)
    monkeypatch.setattr(browser_pane, "is_open", lambda _thread_id: asyncio.sleep(0, result=open_browser))
    monkeypatch.setattr(browser_pane, "watch_page_metadata", lambda *_args: asyncio.Future())
    return FakePane()


@pytest.mark.asyncio
async def test_stream_says_no_browser_yet_without_starting_a_screencast(monkeypatch, screencast):
    pane = _authenticated_pane(monkeypatch, open_browser=False)
    await browser_stream.browser_stream_endpoint(pane, "thread-1")
    assert pane.json == [{"type": "browser_state", "state": "no_browser_yet"}]
    assert pane.closed == [None]
    assert screencast.started == []


@pytest.mark.asyncio
async def test_a_screencast_chrome_refuses_closes_the_pane_with_its_reason(monkeypatch):
    pane = _authenticated_pane(monkeypatch, open_browser=True)
    pane.receive_text = lambda: asyncio.Future()

    async def refused(_thread_id, _token, _deliver):
        raise BrowserSessionError("This page couldn't be shown")

    monkeypatch.setattr(browser_stream, "PANE_RETRY_DELAYS", (0, 0, 0))
    monkeypatch.setattr(browser_pane, "screencast", refused)
    await asyncio.wait_for(browser_stream.browser_stream_endpoint(pane, "thread-1"), 1)
    assert pane.json == [{"type": "browser_state", "state": "error", "reason": "This page couldn't be shown"}]
    assert pane.closed == [None]
    assert not browser_pane_state.has_pane("thread-1")


@pytest.mark.asyncio
async def test_relay_shows_page_frames_and_stops_the_screencast_when_the_pane_leaves(monkeypatch):
    frame = {"type": "frame", "data": "jpeg", "metadata": {"deviceWidth": 400, "deviceHeight": 600}}
    cast = IdleScreencast([frame])
    monkeypatch.setattr(browser_pane, "screencast", cast)
    monkeypatch.setattr(browser_pane, "watch_page_metadata", lambda *_args: asyncio.Future())
    pane, left = FakePane(), asyncio.Event()

    async def receive_text():
        await left.wait()
        raise WebSocketDisconnect()

    pane.receive_text = receive_text
    relay = asyncio.create_task(browser_stream._relay(pane, "thread-1", "user-1"))
    while not pane.text:
        await asyncio.sleep(0)
    assert [json.loads(text) for text in pane.text] == [frame]
    assert cast.stopped == []
    left.set()
    with pytest.raises(WebSocketDisconnect):
        await asyncio.wait_for(relay, 1)
    assert len(cast.started) == 1 and cast.stopped == cast.started


class RecordedScreencast:
    """A pane screencast that records its capture size, holds and restarts into a shared log."""

    def __init__(self, endpoint, log):
        self.endpoint, self.log, self.ended = endpoint, log, asyncio.Event()

    async def run(self):
        await self.ended.wait()

    def capture_at(self, width, height, *, compact=False):
        self.log.append(("capture_at", self.endpoint, width, height, compact))

    def hold(self):
        self.log.append(("hold", self.endpoint))

    async def restart(self):
        self.log.append(("restart", self.endpoint))


@pytest.mark.asyncio
async def test_match_pane_sizes_every_screencast_on_the_thread_before_the_viewport_command(monkeypatch):
    log = []

    class Owner:
        async def run(self, _thread_id, args, **_kwargs):
            log.append(args[:2])
            return "ok"

    monkeypatch.setattr(browser_pane.PROCESS_OWNER, "browser_sessions", lambda: Owner())
    for thread_id, token in (("thread-1", "pane-1"), ("thread-1", "pane-2"), ("thread-2", "pane-3")):
        browser_pane_state.screencast_started(thread_id, token, RecordedScreencast(token, log))
    await browser_pane.match_pane("thread-1", browser_pane.PaneAppearance(680, 1030, 2.0, "dark"))
    # The page is shared, so every pane on its thread captures at the new device size, starting with
    # the restart that the viewport command triggers.
    assert log == [("capture_at", "pane-1", 1360, 2060, False), ("capture_at", "pane-2", 1360, 2060, False),
                   ("set", "viewport"), ("set", "media")]


@pytest.mark.asyncio
async def test_match_pane_marks_a_compact_screencast_for_compact_quality(monkeypatch):
    log = []

    class Owner:
        async def run(self, _thread_id, _args, **_kwargs):
            return "ok"

    monkeypatch.setattr(browser_pane.PROCESS_OWNER, "browser_sessions", lambda: Owner())
    browser_pane_state.screencast_started("thread-1", "pane-1", RecordedScreencast("pane-1", log))
    await browser_pane.match_pane("thread-1", browser_pane.PaneAppearance(393, 852, 2.0, "dark"))
    assert log == [("capture_at", "pane-1", 786, 1704, True)]


@pytest.mark.asyncio
async def test_a_compact_appearance_that_beats_endpoint_resolution_sizes_the_later_screencast(monkeypatch):
    class Owner:
        async def run(self, *_args, **_kwargs):
            return "ok"

    monkeypatch.setattr(browser_pane.PROCESS_OWNER, "browser_sessions", lambda: Owner())
    await browser_pane.match_pane("thread-1", browser_pane.PaneAppearance(393, 732, 2, "dark"))
    log = []
    cast = RecordedScreencast("later", log)
    browser_pane_state.screencast_started("thread-1", "pane", cast)
    assert log == [("capture_at", "later", 786, 1464, True)]
    browser_control.pane_connected("thread-1", "pane", lambda _message: None)
    browser_control.pane_disconnected("thread-1", "pane")
    assert browser_pane_state._capture_sizes == {}


@pytest.mark.asyncio
async def test_each_pane_screencast_is_listed_only_while_it_runs(monkeypatch):
    async def page_endpoint(_thread_id, token, _error):
        return f"page-{token}"

    monkeypatch.setattr(browser_pane, "_page_endpoint", page_endpoint)
    monkeypatch.setattr(browser_pane.browser_screencast, "PaneScreencast", lambda endpoint, _deliver: RecordedScreencast(endpoint, []))
    first = asyncio.create_task(_SCREENCAST("thread-1", "pane-1", lambda _message: None))
    second = asyncio.create_task(_SCREENCAST("thread-1", "pane-2", lambda _message: None))
    while len(browser_pane_state.screencasts("thread-1")) < 2:
        await asyncio.sleep(0)
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    [remaining] = browser_pane_state.screencasts("thread-1")
    assert remaining.endpoint == "page-pane-2"
    remaining.ended.set()
    await second
    assert browser_pane_state.screencasts("thread-1") == []
    assert browser_pane_state._screencasts == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(("args", "restarted"), [
    (("screenshot", "/tmp/shot.jpg"), True),
    (("set", "viewport", "400", "600", "2"), True),
    (("set", "device", "iPhone 15"), True),
    (("set", "media", "dark"), False),
    (("click", "@e1"), False),
    (("snapshot", "-i"), False),
])
async def test_a_page_metrics_command_holds_then_restarts_each_pane_screencast_on_its_thread(args, restarted):
    log = []
    browser_pane_state.screencast_started("thread-1", "pane-1", RecordedScreencast("pane-1", log))
    browser_pane_state.screencast_started("thread-2", "pane-2", RecordedScreencast("pane-2", log))
    browser_pane_state.page_command_starting("thread-1", args)
    await browser_pane_state.page_command_finished("thread-1", args)
    assert log == ([("hold", "pane-1"), ("restart", "pane-1")] if restarted else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_the_command_path_holds_pane_screencasts_during_an_agent_screenshot_and_restarts_them_after(monkeypatch, fails):
    owner = BrowserSessionOwner(namespace="corsia-pane-test")
    runtime = BrowserRuntime(BrowserSessionRecord("thread-1", "test-session"))
    log = []

    async def run_admitted(_runtime, args, **_kwargs):
        log.append(("command", args[0]))
        if fails:
            raise BrowserSessionError("The browser stopped")
        return CommandResult(0, '{"success":true,"data":{}}', ""), False

    async def page_access(*_args, **_kwargs):
        pass

    monkeypatch.setattr(browser_runtime.browser_cli, "check_local_mode", lambda _args: None)
    monkeypatch.setattr(owner, "_pin_native", lambda: asyncio.sleep(0))
    monkeypatch.setattr(owner, "_admit", lambda *_args, **_kwargs: asyncio.sleep(0, result=runtime))
    monkeypatch.setattr(owner, "_run_admitted", run_admitted)
    monkeypatch.setattr(browser_control, "require_page_access", page_access)
    monkeypatch.setattr(browser_cdp, "page_endpoint", lambda _run: asyncio.sleep(0, result="ws://page.test/current"))
    browser_pane_state.screencast_started("thread-1", "pane-1", RecordedScreencast("pane-1", log))
    with pytest.raises(BrowserSessionError) if fails else contextlib.nullcontext():
        await owner.run("thread-1", ("screenshot", "/tmp/shot.jpg"), session="existing")
    # A held pane shows nothing until the restart, so a failed command restarts it too.
    assert log == [("hold", "pane-1"), ("command", "screenshot"), ("restart", "pane-1")]


def _password_page_owner(monkeypatch, opened):
    """A session owner on a page with a visible password field, recording any step the guard opens."""
    owner = BrowserSessionOwner(namespace="corsia-pane-test")
    runtime = BrowserRuntime(BrowserSessionRecord("thread-1", "test-session"))
    answers = {("get", "url"): {"url": "https://login.example.test/"}, ("get", "title"): {"title": "Sign in"},
               ("set", "viewport"): {}, ("set", "media"): {}}

    async def run_admitted(_runtime, args, **_kwargs):
        return CommandResult(0, json.dumps({"success": True, "data": answers[tuple(args[:2])]}), ""), False

    async def password_field(endpoint, _visit, _known):
        return browser_control.PasswordProbe("visit-1", "login.example.test", frozenset({42}), endpoint,
                                             present_nodes=frozenset({42}))

    async def open_step(_thread_id, step):
        browser_control._steps[_thread_id] = step
        browser_control._start_reprobe(_thread_id, step)
        step.node_id = "password-step"
        opened.append(step.visit)

    monkeypatch.setattr(browser_runtime.browser_cli, "check_local_mode", lambda _args: None)
    monkeypatch.setattr(owner, "_pin_native", lambda: asyncio.sleep(0))
    monkeypatch.setattr(owner, "_admit", lambda *_args, **_kwargs: asyncio.sleep(0, result=runtime))
    monkeypatch.setattr(owner, "_run_admitted", run_admitted)
    monkeypatch.setattr(browser_cdp, "page_password_probe", password_field)
    monkeypatch.setattr(browser_cdp, "page_endpoint", lambda _run: asyncio.sleep(0, result="ws://page.test/current"))
    monkeypatch.setattr(browser_control, "_open_step", open_step)
    monkeypatch.setattr(browser_control.state, "broadcast_to_thread", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(browser_pane.PROCESS_OWNER, "browser_sessions", lambda: owner)
    return owner


@pytest.mark.asyncio
async def test_the_pane_reads_its_labels_and_sizes_a_password_page_without_the_agent_guard(monkeypatch):
    opened = []
    owner = _password_page_owner(monkeypatch, opened)
    # The person navigated their pane to a sign-in page: its address and tab label are theirs to see,
    # and their pane still sizes the page. No agent asked, so no password step opens.
    assert await browser_pane.page_info("thread-1") == ("https://login.example.test/", "Sign in")
    await browser_pane.match_pane("thread-1", browser_pane.PaneAppearance(680, 1030, 2.0, "dark"))
    assert opened == []
    # The agent's own read of the same page is refused and opens the person's step.
    with pytest.raises(BrowserSessionError, match="asks for a password"):
        await owner.run("thread-1", ("get", "url"), session="existing")
    assert opened == ["visit-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [("snapshot",), ("eval", "document.title"), ("get", "value", "42"), ("screenshot", "/tmp/x.png")])
async def test_a_pane_command_is_only_navigation_labels_or_page_size(monkeypatch, args):
    owner = _password_page_owner(monkeypatch, [])
    with pytest.raises(BrowserSessionError, match="only navigates"):
        await owner.run("thread-1", args, pane=True, session="existing")


