"""The pane's agent presence and field marker: which commands put the agent in the page, and where it wrote."""
import asyncio
import json

import pytest

from host_access import browser_cdp, browser_control, browser_pane
from host_access.browser_cli import CommandResult
from host_access.browser_session_state import BrowserRuntime, BrowserSessionError, BrowserSessionRecord
from host_access.browser_sessions import BrowserSessionOwner
from tools import browser_runtime


@pytest.fixture(autouse=True)
def _no_presence_state():
    for registry in (browser_control._panes, browser_control._pane_reports, browser_control._agent_seen):
        registry.clear()
    yield
    for registry in (browser_control._panes, browser_control._pane_reports, browser_control._agent_seen):
        registry.clear()


def _owner(monkeypatch, password_page):
    owner = BrowserSessionOwner(namespace="corsia-presence-test")
    runtime = BrowserRuntime(BrowserSessionRecord("thread-1", "test-session"))

    async def run_admitted(_runtime, _args, **_kwargs):
        return CommandResult(0, json.dumps({"success": True, "data": {"url": "https://example.test/"}}), ""), False

    async def page_access(_thread_id, _probe, _args):
        if password_page["shown"]:
            raise BrowserSessionError(browser_control.PASSWORD_PAGE)

    monkeypatch.setattr(browser_runtime.browser_cli, "check_local_mode", lambda _args: None)
    monkeypatch.setattr(owner, "_pin_native", lambda: asyncio.sleep(0))
    monkeypatch.setattr(owner, "_admit", lambda *_args, **_kwargs: asyncio.sleep(0, result=runtime))
    monkeypatch.setattr(owner, "_run_admitted", run_admitted)
    monkeypatch.setattr(browser_control, "require_page_access", page_access)
    return owner


def _agent_messages(messages):
    return [message for message in messages if message["type"] == "browser_agent"]


@pytest.mark.asyncio
async def test_only_an_agent_command_the_guard_lets_through_puts_the_agent_in_the_page(monkeypatch):
    password_page = {"shown": False}
    owner = _owner(monkeypatch, password_page)
    messages = []
    browser_control.pane_connected("thread-1", "pane-1", messages.append)

    # The person's own pane commands are not the agent, the first open from a pane with no stream included.
    monkeypatch.setattr(browser_pane.PROCESS_OWNER, "browser_sessions", lambda: owner)
    await browser_pane.open_url("thread-1", "https://example.test/")
    await owner.run("thread-1", ("get", "url"), control_token="pane-1", session="existing")
    assert _agent_messages(messages) == []

    await owner.run("thread-1", ("get", "url"), session="existing")
    assert _agent_messages(messages) == [{"type": "browser_agent", "age_ms": 0, "field": None, "label": None}]

    # On a password page the agent is refused, so it is not in the page (S4).
    password_page["shown"] = True
    with pytest.raises(BrowserSessionError, match="asks for a password"):
        await owner.run("thread-1", ("snapshot",), session="existing")
    assert len(_agent_messages(messages)) == 1


@pytest.mark.asyncio
async def test_a_pane_that_connects_later_shows_how_long_ago_the_agent_acted_until_the_browser_closes(monkeypatch):
    owner = _owner(monkeypatch, {"shown": False})
    await owner.run("thread-1", ("get", "url"), session="existing")
    later = []
    browser_control.pane_connected("thread-1", "pane-2", later.append)
    [presence] = _agent_messages(later)
    assert presence["field"] is None and 0 <= presence["age_ms"] < 1000

    browser_control.browser_closed("thread-1")
    reopened = []
    browser_control.pane_connected("thread-1", "pane-3", reopened.append)
    assert _agent_messages(reopened) == []


class FakeCdp:
    """A page whose agent target is node 7, with a label above it when ``labelled``."""

    def __init__(self, labelled):
        self.labelled = labelled
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def call(self, method, **params):
        self.calls.append(method)
        if method == "DOM.getNodeForLocation":
            return {"backendNodeId": 7}
        if method == "DOM.describeNode":
            return {"node": {"backendNodeId": 7}}
        if method == "DOM.getBoxModel" and params.get("backendNodeId") == 7:
            return {"model": {"border": [20, 60, 220, 60, 220, 90, 20, 90]}}
        if method == "DOM.getBoxModel" and params.get("objectId") == "label":
            return {"model": {"border": [20, 36, 80, 36, 80, 54, 20, 54]}}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "main"}}}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 3}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "field"}}
        if method == "Runtime.callFunctionOn" and "labels" in params["functionDeclaration"]:
            return {"result": {"type": "object", "objectId": "label"} if self.labelled else {"type": "object", "subtype": "null"}}
        return {}


async def _run(args, _timeout):
    if tuple(args[:2]) == ("get", "box"):
        return CommandResult(0, json.dumps({"data": {"x": 20, "y": 60, "width": 200, "height": 30}}), "")
    return CommandResult(0, json.dumps({"data": {"cdpUrl": "ws://browser.test/devtools/browser/1"}}), "")


@pytest.mark.asyncio
@pytest.mark.parametrize("labelled", [True, False])
async def test_a_shared_fill_reports_its_fields_box_and_its_labels_in_page_pixels(monkeypatch, labelled):
    cdp = FakeCdp(labelled)
    monkeypatch.setattr(browser_cdp, "CdpClient", lambda _endpoint: cdp)
    monkeypatch.setattr(browser_cdp, "_page_endpoint", lambda _url: "ws://page.test/devtools/page/1")
    marked = []
    assert await browser_cdp.shared_action(_run, ("fill", "@e2", "agent value"), lambda field, label: marked.append((field, label))) == "ok"
    label = {"x": 20, "y": 36, "width": 60, "height": 18} if labelled else None
    assert marked == [({"x": 20, "y": 60, "width": 200, "height": 30}, label)]


@pytest.mark.asyncio
async def test_a_shared_click_marks_no_field(monkeypatch):
    cdp = FakeCdp(labelled=True)
    monkeypatch.setattr(browser_cdp, "CdpClient", lambda _endpoint: cdp)
    monkeypatch.setattr(browser_cdp, "_page_endpoint", lambda _url: "ws://page.test/devtools/page/1")
    marked = []
    assert await browser_cdp.shared_action(_run, ("click", "@e2"), lambda field, label: marked.append((field, label))) == "ok"
    assert marked == []
