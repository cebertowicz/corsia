import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, WebSocketDisconnect

from api import browser
from host_access import browser_control
from host_access.browser_cli import CommandResult
from host_access.browser_session_state import BrowserRuntime, BrowserSessionRecord
from host_access.browser_sessions import BrowserSessionOwner
from ws import browser_stream


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
    assert not browser_control.has_user_control("thread-1")


def test_control_belongs_to_its_connection_and_releases_on_end():
    assert browser_control.take_control("thread-1", "first")
    assert not browser_control.hand_back("thread-1", "second")
    assert browser_control.holds_control("thread-1", "first")
    assert browser_control.hand_back("thread-1", "first")
    assert not browser_control.has_user_control("thread-1")


@pytest.mark.asyncio
async def test_stream_port_distinguishes_no_session_from_failure(monkeypatch):
    owner = BrowserSessionOwner(namespace="corsia-pane-test")
    assert (await owner.stream_port("thread-1")).no_session

    runtime = BrowserRuntime(BrowserSessionRecord("thread-1", "test-session"))
    owner._sessions["thread-1"] = runtime
    monkeypatch.setattr(owner, "_pin_native", lambda: asyncio.sleep(0))
    monkeypatch.setattr(owner, "_observe", lambda _runtime: asyncio.sleep(0, result=SimpleNamespace(alive=True, artifacts_absent=True)))
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
