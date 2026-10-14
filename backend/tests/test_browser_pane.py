import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, WebSocketDisconnect

from api import browser
from host_access import browser_control
from host_access.browser_cli import CommandResult
from host_access.browser_session_state import BrowserRuntime, BrowserSessionError, BrowserSessionRecord
from host_access.browser_sessions import BrowserSessionOwner
from tools import browser_runtime
from ws import browser_stream


@pytest.fixture(autouse=True)
def _no_control_holders():
    browser_control._control_holders.clear()
    yield
    browser_control._control_holders.clear()


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


class FakeStream:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.Future()


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


@pytest.mark.asyncio
async def test_relay_drops_input_without_control_and_forwards_only_allowed_input():
    pane = FakePane([
        json.dumps({"type": "input_mouse"}),
        json.dumps({"type": "unexpected"}),
    ])
    stream = FakeStream()
    with pytest.raises(WebSocketDisconnect):
        await browser_stream._relay(pane, stream, "thread-1", "user-1")
    assert stream.sent == [json.dumps({"type": "screencast_start"})]

    pane = FakePane([
        json.dumps({"type": "take_over"}),
        json.dumps({"type": "input_mouse", "x": 1}),
        json.dumps({"type": "input_keyboard", "key": "a"}),
        json.dumps({"type": "unexpected"}),
        json.dumps({"type": "hand_back"}),
    ])
    stream = FakeStream()
    with pytest.raises(WebSocketDisconnect):
        await browser_stream._relay(pane, stream, "thread-1", "user-1")
    assert stream.sent == [
        json.dumps({"type": "screencast_start"}),
        json.dumps({"type": "input_mouse", "x": 1}),
        json.dumps({"type": "input_keyboard", "key": "a"}),
    ]
    browser_control.require_page_control("thread-1")


def test_control_belongs_to_its_connection_and_releases_on_end():
    assert browser_control.take_control("thread-1", "first")
    assert not browser_control.hand_back("thread-1", "second")
    assert browser_control.holds_control("thread-1", "first")
    assert browser_control.hand_back("thread-1", "first")
    browser_control.require_page_control("thread-1")


@pytest.mark.asyncio
async def test_page_commands_run_only_for_the_control_holder(monkeypatch):
    owner = BrowserSessionOwner(namespace="corsia-pane-test")
    browser_control.take_control("thread-1", "pane-1")
    for token in (None, "pane-2"):
        with pytest.raises(BrowserSessionError, match="taken control"):
            await owner.run("thread-1", ("open", "https://example.test"), control_token=token)

    def past_the_gate(_args):
        raise RuntimeError("past the control gate")

    monkeypatch.setattr(browser_runtime.browser_cli, "check_local_mode", past_the_gate)
    with pytest.raises(RuntimeError, match="past the control gate"):
        await owner.run("thread-1", ("open", "https://example.test"), control_token="pane-1")


@pytest.mark.asyncio
async def test_streamless_pane_open_only_starts_a_new_session():
    owner = BrowserSessionOwner(namespace="corsia-pane-test")
    owner._sessions["thread-1"] = BrowserRuntime(BrowserSessionRecord("thread-1", "test-session"))
    with pytest.raises(BrowserSessionError, match="already open"):
        await owner._admit("thread-1", lease_id=None, new_session_only=True)


@pytest.mark.asyncio
async def test_agent_close_waits_while_the_user_controls(monkeypatch):
    closed = []
    monkeypatch.setattr(browser_runtime.PROCESS_OWNER, "close_browser", lambda thread_id: closed.append(thread_id))
    browser_control.take_control("thread-1", "pane-1")
    message = await browser_runtime.BrowserCloseTool().execute(_thread_id="thread-1")
    assert message.startswith("Error: the user has taken control")
    assert not message.startswith("Error: Error:")
    assert closed == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("returncode", "announced"), [(0, ["thread-1"]), (1, [])])
async def test_browser_opened_is_announced_only_after_a_successful_open(monkeypatch, returncode, announced):
    owner = BrowserSessionOwner(namespace="corsia-pane-test")
    runtime = BrowserRuntime(BrowserSessionRecord("thread-1", "test-session"))
    seen = []
    monkeypatch.setattr(browser_runtime.browser_cli, "check_local_mode", lambda _args: None)
    monkeypatch.setattr(owner, "_pin_native", lambda: asyncio.sleep(0))
    monkeypatch.setattr(owner, "_admit", lambda *_args, **_kwargs: asyncio.sleep(0, result=runtime))
    monkeypatch.setattr(owner, "_run_admitted", lambda *_args, **_kwargs: asyncio.sleep(
        0, result=(CommandResult(returncode, "", "failed"), False)))
    monkeypatch.setattr(browser_control, "announce_browser_opened", seen.append)
    await owner.run("thread-1", ("open", "https://example.test"))
    assert seen == announced


@pytest.mark.asyncio
async def test_navigate_requires_this_connections_control(monkeypatch):
    opened = []

    async def open_url(thread_id, url, token):
        opened.append((thread_id, url, token))
        return "ok"

    monkeypatch.setattr(browser_stream.browser_pane, "open_url", open_url)
    refused = await browser_stream._navigate("thread-1", "pane-1", "https://example.test")
    assert refused["error"] == "Take over to navigate"
    browser_control.take_control("thread-1", "pane-1")
    assert (await browser_stream._navigate("thread-1", "pane-1", "file:///etc/hosts"))["error"]
    assert (await browser_stream._navigate("thread-1", "pane-1", "https://example.test"))["error"] is None
    assert opened == [("thread-1", "https://example.test", "pane-1")]


@pytest.mark.asyncio
async def test_stream_port_distinguishes_no_session_from_failure(monkeypatch):
    owner = BrowserSessionOwner(namespace="corsia-pane-test")
    assert (await owner.stream_port("thread-1")).no_session

    runtime = BrowserRuntime(BrowserSessionRecord("thread-1", "test-session"))
    owner._sessions["thread-1"] = runtime
    monkeypatch.setattr(owner, "_pin_native", lambda: asyncio.sleep(0))
    monkeypatch.setattr(owner, "_observe", lambda _runtime: asyncio.sleep(0, result=SimpleNamespace(alive=True, artifacts_absent=True)))
    monkeypatch.setattr(owner, "_observe_attempt", lambda _runtime: asyncio.sleep(0))
    monkeypatch.setattr(owner, "_session_argv", lambda _runtime, args: args)

    async def invoke(args, **_kwargs):
        if args == ("stream", "status"):
            return CommandResult(0, '{"success":true,"data":{"enabled":false,"port":null}}', "")
        return CommandResult(1, "", "failed")

    monkeypatch.setattr(owner, "_invoke", invoke)
    failed = await owner.stream_port("thread-1")
    assert failed.port is None
    assert failed.error == "browser stream could not be enabled"


def _streaming_owner(monkeypatch, responses):
    owner = BrowserSessionOwner(namespace="corsia-pane-test")
    owner._sessions["thread-1"] = BrowserRuntime(BrowserSessionRecord("thread-1", "test-session"))
    calls = []
    monkeypatch.setattr(owner, "_pin_native", lambda: asyncio.sleep(0))
    monkeypatch.setattr(owner, "_observe", lambda _runtime: asyncio.sleep(0, result=SimpleNamespace(alive=True, artifacts_absent=True)))
    monkeypatch.setattr(owner, "_observe_attempt", lambda _runtime: asyncio.sleep(0))
    monkeypatch.setattr(owner, "_close_runtime", lambda runtime: asyncio.sleep(0, result=calls.append("reset")))
    monkeypatch.setattr(owner, "_session_argv", lambda _runtime, args: args)

    async def invoke(args, **_kwargs):
        calls.append(args)
        return responses.pop(0)

    monkeypatch.setattr(owner, "_invoke", invoke)
    return owner, calls


@pytest.mark.asyncio
async def test_stream_port_reuses_a_stream_the_daemon_already_enabled(monkeypatch):
    owner, calls = _streaming_owner(monkeypatch, [
        CommandResult(0, '{"success":true,"data":{"enabled":true,"port":4567}}', ""),
    ])
    result = await owner.stream_port("thread-1")
    assert (result.port, result.error) == (4567, None)
    assert calls == [("stream", "status")]


@pytest.mark.asyncio
async def test_stream_port_enables_streaming_when_it_is_off(monkeypatch):
    owner, calls = _streaming_owner(monkeypatch, [
        CommandResult(0, '{"success":true,"data":{"enabled":false,"port":null}}', ""),
        CommandResult(0, '{"success":true,"data":{"enabled":true}}', ""),
        CommandResult(0, '{"success":true,"data":{"enabled":true,"port":4567}}', ""),
    ])
    result = await owner.stream_port("thread-1")
    assert (result.port, result.error) == (4567, None)
    assert calls == [("stream", "status"), ("stream", "enable"), ("stream", "status")]


@pytest.mark.asyncio
async def test_timed_out_stream_enable_resets_the_session_like_any_command(monkeypatch):
    owner, calls = _streaming_owner(monkeypatch, [
        CommandResult(0, '{"success":true,"data":{"enabled":false,"port":null}}', ""),
        CommandResult(-1, "", "", timed_out=True),
    ])
    result = await owner.stream_port("thread-1")
    assert result.error == "browser stream could not be enabled"
    assert calls == [("stream", "status"), ("stream", "enable"), "reset"]
    assert owner._sessions["thread-1"].record.terminal_state == "resetting"
    assert owner.active_command_count() == 0
