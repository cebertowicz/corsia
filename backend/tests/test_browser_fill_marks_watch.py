"""The saved-login fill's marks with the real page watch (browser_fill_marks.agent_command over AgentWrites).

A script command's page watch must see every document the page showed while the command ran, so a history restore of
the first document (which comes back under its own loader id) can't hide a document committed in between (#3361).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from host_access import browser_fill_marks as marks, browser_login_capture_writes as watch_module, browser_login_fill

THREAD = "thread-marks-watch"


@pytest.fixture(autouse=True)
def _fresh():
    marks.forget(THREAD)
    yield
    marks.forget(THREAD)


class Tab:
    """One page target: its main document, the browser's page targets, and the watch sockets on it."""

    def __init__(self) -> None:
        self.loader, self.pages = "L1", {"page-1"}
        self.queues: list[asyncio.Queue] = []

    def client(self, _endpoint: str) -> TabClient:
        return TabClient(self)

    def navigate(self, loader: str) -> None:
        """A main-frame commit, or a back/forward cache restore, which brings a document back under its own loader."""
        self.loader = loader
        for queue in self.queues:
            queue.put_nowait({"method": "Page.frameNavigated", "params": {"frame": {"id": "top", "loaderId": loader}}})


class TabClient:
    """A CDP connection to the tab, as the page watch uses it: calls, and a socket it alone reads once started."""

    def __init__(self, tab: Tab) -> None:
        self.tab, self.sent, self.socket = tab, 0, self
        self.queue: asyncio.Queue = asyncio.Queue()

    async def __aenter__(self) -> TabClient:
        return self

    async def __aexit__(self, *_args) -> None:
        if self.queue in self.tab.queues:
            self.tab.queues.remove(self.queue)

    async def recv(self) -> str:
        return json.dumps(await self.queue.get())

    def next_request(self) -> int:
        return self.sent + 1

    async def send(self, method: str) -> int:
        self.sent += 1
        self.queue.put_nowait({"id": self.sent, "result": await self.call(method)})
        return self.sent

    async def call(self, method: str) -> dict:
        if method == "Page.enable":
            self.tab.queues.append(self.queue)
            return {}
        assert method == "Page.getFrameTree"
        return {"frameTree": {"frame": {"id": "top", "loaderId": self.tab.loader}}}


@pytest.fixture
def tab(monkeypatch) -> Tab:
    tab = Tab()

    async def page(_endpoint):
        return tab.loader, "example.com"

    monkeypatch.setattr(marks, "_page", page)
    monkeypatch.setattr(watch_module, "CdpClient", tab.client)
    monkeypatch.setattr(watch_module, "_page_ids", lambda _endpoint, _timeout: frozenset(tab.pages))
    return tab


async def _refused(document: str, site: str) -> bool:
    # A marked session refuses before the fill opens any connection of its own.
    return not await browser_login_fill._clean(THREAD, "s1", document, site, "ws://browser", "ws://page")


@pytest.mark.asyncio
async def test_a_history_restore_cant_hide_the_document_a_script_ran_in(tab):
    # The script runs in a second document on another site; history then restores the first from the back/forward
    # cache under its own loader, before the command ends and the page is read again.
    async with marks.agent_command(THREAD, "s1", "ws://page", ("eval", "1")) as finished:
        tab.navigate("L2")
        tab.navigate("L1")
        finished(True)
    assert tab.loader == "L1" and marks.session_marked(THREAD, "s1")
    assert await _refused("L2", "other.test")
    assert not tab.queues  # the watch's connection is closed


@pytest.mark.asyncio
async def test_a_script_that_stayed_in_its_document_leaves_the_session_unmarked(tab):
    async with marks.agent_command(THREAD, "s1", "ws://page", ("wait", "--fn", "window.ready")) as finished:
        finished(True)
    assert not marks.session_marked(THREAD, "s1") and marks.scripted(THREAD, "L1")
    assert not marks.scripted(THREAD, "L2") and not marks.site_marked(THREAD, "s1", "other.test")
    assert await _refused("L1", "example.com")
    assert not tab.queues


@pytest.mark.asyncio
async def test_a_page_that_opened_during_a_script_marks_the_session(tab):
    async with marks.agent_command(THREAD, "s1", "ws://page", ("eval", "1")) as finished:
        tab.pages.add("page-2")
        finished(True)
    assert marks.session_marked(THREAD, "s1")


@pytest.mark.asyncio
async def test_a_watch_that_cant_connect_marks_the_session(tab, monkeypatch):
    class Unreachable:
        def __init__(self, _endpoint):
            pass

        async def __aenter__(self):
            raise OSError("refused")

    monkeypatch.setattr(watch_module, "CdpClient", Unreachable)
    async with marks.agent_command(THREAD, "s1", "ws://page", ("eval", "1")) as finished:
        finished(True)
    assert marks.session_marked(THREAD, "s1")


@pytest.mark.asyncio
async def test_a_cancelled_watch_start_marks_the_session_and_cancels(tab, monkeypatch):
    class Stalled:
        def __init__(self, _endpoint):
            pass

        async def __aenter__(self):
            raise asyncio.CancelledError

    monkeypatch.setattr(watch_module, "CdpClient", Stalled)
    with pytest.raises(asyncio.CancelledError):
        async with marks.agent_command(THREAD, "s1", "ws://page", ("eval", "1")):
            pytest.fail("the command must not run once its watch was cancelled")
    assert marks.session_marked(THREAD, "s1")
