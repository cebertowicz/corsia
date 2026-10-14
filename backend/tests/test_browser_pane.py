import pytest
from fastapi import HTTPException

from api.browser import _http_url
from host_access.browser_cli import CommandResult
from host_access.browser_session_state import BrowserRuntime, BrowserSessionError, BrowserSessionRecord
from host_access.browser_sessions import BrowserSessionOwner


def test_open_url_accepts_http_and_https_only():
    assert _http_url("https://example.test/path") == "https://example.test/path"
    assert _http_url("http://example.test") == "http://example.test"
    for url in ("file:///tmp/x", "javascript:alert(1)", "https:"):
        with pytest.raises(HTTPException) as error:
            _http_url(url)
        assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_user_control_refuses_agent_command_before_browser_startup():
    owner = BrowserSessionOwner(namespace="corsia-pane-test")
    await owner.set_user_control("thread-1", True)
    with pytest.raises(BrowserSessionError, match="user has taken control"):
        await owner.run("thread-1", ("snapshot",))
    await owner.set_user_control("thread-1", False)
    assert not owner.has_user_control("thread-1")


@pytest.mark.asyncio
async def test_stream_port_reads_the_native_json_data_payload(monkeypatch):
    owner = BrowserSessionOwner(namespace="corsia-pane-test")
    runtime = BrowserRuntime(BrowserSessionRecord("thread-1", "test-session"))
    owner._sessions["thread-1"] = runtime

    calls = []

    async def invoke(args, **_kwargs):
        calls.append(args)
        return CommandResult(0, '{"success":true,"data":{"port":4567}}', "")

    monkeypatch.setattr(owner, "_invoke", invoke)
    monkeypatch.setattr(owner, "_session_argv", lambda _runtime, args: args)
    assert await owner.stream_port("thread-1") == 4567
    assert calls == [("stream", "enable"), ("stream", "status")]
