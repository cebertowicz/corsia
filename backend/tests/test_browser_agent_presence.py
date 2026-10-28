"""The pane's agent presence and field marker: which commands put the agent in the page, and where it wrote."""
import asyncio
import json

import pytest

from host_access import browser_cdp, browser_control, browser_dom_write, browser_pane, browser_pane_state
from host_access.browser_cli import CommandResult
from host_access.browser_session_state import BrowserRuntime, BrowserSessionError, BrowserSessionRecord
from host_access.browser_sessions import BrowserSessionOwner
from tools import browser_runtime


@pytest.fixture(autouse=True)
def _no_presence_state():
    for registry in (browser_pane_state._panes, browser_pane_state._pane_reports, browser_pane_state._agent_seen):
        registry.clear()
    yield
    for registry in (browser_pane_state._panes, browser_pane_state._pane_reports, browser_pane_state._agent_seen):
        registry.clear()


def _owner(monkeypatch, password_page):
    owner = BrowserSessionOwner(namespace="corsia-presence-test")
    runtime = BrowserRuntime(BrowserSessionRecord("thread-1", "test-session"))

    async def run_admitted(_runtime, _args, **_kwargs):
        return CommandResult(0, json.dumps({"success": True, "data": {"url": "https://example.test/"}}), ""), False

    async def page_access(_thread_id, _probe, _args, **_kwargs):
        if password_page["shown"]:
            raise BrowserSessionError(browser_control.PASSWORD_PAGE)

    monkeypatch.setattr(browser_runtime.browser_cli, "check_local_mode", lambda _args: None)
    monkeypatch.setattr(owner, "_pin_native", lambda: asyncio.sleep(0))
    monkeypatch.setattr(owner, "_admit", lambda *_args, **_kwargs: asyncio.sleep(0, result=runtime))
    monkeypatch.setattr(owner, "_run_admitted", run_admitted)
    monkeypatch.setattr(browser_control, "require_page_access", page_access)
    monkeypatch.setattr(browser_cdp, "page_endpoint", lambda _run: asyncio.sleep(0, result="ws://page.test/devtools/page/1"))
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
    await owner.run("thread-1", ("get", "url"), pane=True, session="existing")
    assert _agent_messages(messages) == []

    await owner.run("thread-1", ("get", "url"), session="existing")
    assert _agent_messages(messages) == [{"type": "browser_agent", "age_ms": 0, "field": None, "label": None}]

    # On a password page the agent is refused, so it is not in the page (S4).
    password_page["shown"] = True
    with pytest.raises(BrowserSessionError, match="asks for a password"):
        await owner.run("thread-1", ("snapshot",), session="existing")
    assert len(_agent_messages(messages)) == 1


@pytest.mark.asyncio
async def test_native_ref_click_is_refused_before_input_when_person_has_control(monkeypatch):
    owner = _owner(monkeypatch, {"shown": False})
    browser_control.pane_connected("thread-1", "pane-1")
    native_calls = []

    async def refuse_person_step(_thread_id, _probe, _args, **_kwargs):
        raise BrowserSessionError(browser_control.PERSON_STEP)

    async def native_input(_runtime, args, **_kwargs):
        native_calls.append(args)
        return CommandResult(0, "{}", ""), False

    monkeypatch.setattr(browser_control, "require_page_access", refuse_person_step)
    monkeypatch.setattr(owner, "_run_admitted", native_input)
    with pytest.raises(BrowserSessionError, match=browser_control.PERSON_STEP):
        await owner.run("thread-1", ("click", "@e1"), session="existing")
    assert native_calls == []


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

    def __init__(self, labelled, write=None, viewport=(800, 600), scroll=(0, 0)):
        self.labelled = labelled
        self.write = write if write is not None else {"result": {"type": "string", "value": "ok"}}
        self.viewport = viewport
        self.scroll = scroll
        self.calls = []
        self.hit_tests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def call(self, method, **params):
        self.calls.append(method)
        if method == "DOM.getNodeForLocation":
            self.hit_tests.append((params["x"], params["y"]))
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
        if method == "Runtime.callFunctionOn":
            return self.write
        if method == "Page.getLayoutMetrics":
            return {"cssVisualViewport": {"clientWidth": self.viewport[0], "clientHeight": self.viewport[1],
                                          "pageX": self.scroll[0], "pageY": self.scroll[1]}}
        return {}


async def _run(args, _timeout):
    if tuple(args[:2]) == ("get", "box"):
        return CommandResult(0, json.dumps({"data": {"x": 20, "y": 60, "width": 200, "height": 30}}), "")
    return CommandResult(0, json.dumps({"data": {"cdpUrl": "ws://browser.test/devtools/browser/1"}}), "")


@pytest.mark.asyncio
@pytest.mark.parametrize("labelled", [True, False])
async def test_a_shared_fill_reports_its_fields_box_and_its_labels_in_page_pixels(monkeypatch, labelled):
    cdp = FakeCdp(labelled)
    monkeypatch.setattr(browser_dom_write, "CdpClient", lambda _endpoint: cdp)
    marked = []
    assert await browser_dom_write.shared_action(_run, "ws://page.test", ("fill", "@e2", "agent value"),
                                           lambda field, label: marked.append((field, label))) == "ok"
    label = {"x": 20, "y": 36, "width": 60, "height": 18} if labelled else None
    assert marked == [({"x": 20, "y": 60, "width": 200, "height": 30}, label)]


@pytest.mark.asyncio
async def test_a_shared_click_marks_no_field(monkeypatch):
    cdp = FakeCdp(labelled=True)
    monkeypatch.setattr(browser_dom_write, "CdpClient", lambda _endpoint: cdp)
    marked = []
    assert await browser_dom_write.shared_action(_run, "ws://page.test", ("click", "@e2"),
                                           lambda field, label: marked.append((field, label))) == "ok"
    assert marked == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("write", "reason"), [
    # A page function that throws reports exceptionDetails, not a protocol error.
    ({"result": {"type": "object", "subtype": "error"}, "exceptionDetails": {"text": "Uncaught"}}, "page_error"),
    ({"result": {"type": "string", "value": "no_option"}}, "no_option"),
    ({"result": {"type": "string", "value": "rich_text"}}, "rich_text"),
    ({"result": {"type": "string", "value": "rejected_value"}}, "rejected_value"),
    ({"result": {"type": "undefined"}}, "page_error"),
])
async def test_a_shared_write_the_page_did_not_apply_is_refused_with_its_reason(monkeypatch, write, reason):
    cdp = FakeCdp(labelled=False, write=write)
    monkeypatch.setattr(browser_dom_write, "CdpClient", lambda _endpoint: cdp)
    marked = []
    with pytest.raises(browser_dom_write.DomWriteRefused) as refused:
        await browser_dom_write.shared_action(_run, "ws://page.test", ("select", "@e2", "Missing"),
                                        lambda field, label: marked.append((field, label)))
    assert refused.value.reason == reason and "Nothing was sent as real input" in str(refused.value)
    assert marked == []


@pytest.mark.asyncio
async def test_a_read_target_outside_the_viewport_is_not_located_and_the_page_is_not_scrolled(monkeypatch):
    cdp = FakeCdp(labelled=False, viewport=(100, 50))
    monkeypatch.setattr(browser_dom_write, "CdpClient", lambda _endpoint: cdp)
    commands = []

    async def run(args, timeout):
        commands.append(tuple(args))
        return await _run(args, timeout)

    assert await browser_dom_write.target_node(run, "ws://page.test", "@e2") is None
    assert ("scrollintoview", "@e2") not in commands
    cdp.viewport = (800, 600)
    assert await browser_dom_write.target_node(_run, "ws://page.test", "@e2") == 7


@pytest.mark.asyncio
@pytest.mark.parametrize(("uncovered", "expected"), [(True, 7), (False, None)])
async def test_css_target_uses_hit_identity(uncovered, expected):
    class Target(FakeCdp):
        async def call(self, method, **params):
            if method == "Runtime.evaluate":
                return {"result": {"objectId": "target"}}
            if method == "Runtime.callFunctionOn" and params.get("objectId") == "target":
                return {"result": {"value": uncovered}}
            if method == "DOM.describeNode" and params.get("objectId") == "target":
                return {"node": {"backendNodeId": 7}}
            return await super().call(method, **params)

    cdp = Target(labelled=False)
    found, node = await browser_dom_write._css_target(cdp, "#button", {"x": 20, "y": 60, "width": 200, "height": 30})
    assert found and node == expected


@pytest.mark.parametrize("selector", ["@e1", "e1", "ref=e1"])
def test_ref_click_uses_native_actionability(selector):
    assert browser_dom_write.native_ref_click(("click", selector))
    assert not browser_dom_write.native_ref_click(("fill", selector, "value"))
    assert not browser_dom_write.native_ref_click(("click", "#button"))


@pytest.mark.asyncio
async def test_a_scrolled_page_hit_tests_the_target_in_document_pixels(monkeypatch):
    """The box and the box model are in viewport pixels; DOM.getNodeForLocation takes document pixels."""
    cdp = FakeCdp(labelled=False, scroll=(5, 1905))
    monkeypatch.setattr(browser_dom_write, "CdpClient", lambda _endpoint: cdp)
    assert await browser_dom_write.shared_action(_run, "ws://page.test", ("click", "@e2")) == "ok"
    assert cdp.hit_tests == [(5 + 120, 1905 + 75)]
