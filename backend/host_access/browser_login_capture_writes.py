"""Mark every document an agent command that can type passes through (capture rule 7).

A second CDP connection to the agent's page sees each main-frame Page.frameNavigated while the command runs,
back/forward cache restores included, and a restored document keeps its loader id (checked on Chrome for
Testing 152). So the start document, each document the page shows and the end document are all marked,
whatever loads the command caused. A click can also open a new page, and agent-browser runs the rest of the
command there (checked on Chrome for Testing 152), out of this watch's sight: so a page that appears while the
command runs marks every document of the session, as does a failure anywhere, cancel included.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen

from host_access.browser_cdp import CdpClient
from host_access.browser_login_capture_cdp import quiet_cdp

log = logging.getLogger(__name__)
# Stands for "a document whose loader couldn't be read": the thread's every document counts as marked.
ANY_DOCUMENT = ""


def _loader(frame: object) -> str:
    loader = frame.get("loaderId") if isinstance(frame, dict) else None
    return loader if isinstance(loader, str) and loader else ANY_DOCUMENT


def _page_ids(endpoint: str, timeout: float) -> frozenset[str]:
    """Every page target's id in the agent's browser, read from the CDP port the watched page is on."""
    listing = urlunsplit(("http", urlsplit(endpoint).netloc, "/json/list", "", ""))
    with urlopen(listing, timeout=timeout) as response:  # nosec B310 - loopback CDP endpoint from agent-browser
        targets = json.load(response)
    if not isinstance(targets, list):
        raise ValueError("the page list isn't a list")
    return frozenset(str(target.get("id")) for target in targets
                     if isinstance(target, dict) and target.get("type") == "page")


class AgentWrites:
    """One agent command's watch: start it before the command runs, settle it before the pane gets the page back."""

    def __init__(self, mark: Callable[[str], None], timeout: float, done: Callable[[], None] = lambda: None) -> None:
        self._mark, self._timeout, self._done = mark, timeout, done
        self._cdp: CdpClient | None = None
        self._listener: asyncio.Task[dict] | None = None
        self._answer: int | None = None
        self._settled = False
        self._endpoint, self._pages = "", frozenset[str]()

    async def start(self, endpoint: str) -> None:
        """Mark the current document and watch for the next ones; on any failure mark every document."""
        try:
            self._endpoint, self._cdp = endpoint, quiet_cdp(endpoint)
            await asyncio.wait_for(self._open(self._cdp), self._timeout)
        except BaseException as exc:
            self._fail(exc)
            await self._close()
            if not isinstance(exc, Exception):
                raise

    async def _open(self, cdp: CdpClient) -> None:
        await cdp.__aenter__()
        await cdp.call("Page.enable")
        self._mark(_loader(((await cdp.call("Page.getFrameTree")).get("frameTree") or {}).get("frame")))
        self._pages = await asyncio.to_thread(_page_ids, self._endpoint, self._timeout)
        self._listener = asyncio.create_task(self._listen(cdp))

    async def _listen(self, cdp: CdpClient) -> dict:
        # The only reader of this socket once started; it also returns settle's final frame tree, so no
        # navigation event that arrives before that answer is lost.
        while True:
            message = json.loads(await cdp.socket.recv())
            params = message.get("params") or {}
            if message.get("method") == "Page.frameNavigated" and isinstance(params, dict):
                frame = params.get("frame")
                if not (isinstance(frame, dict) and frame.get("parentId")):
                    self._mark(_loader(frame))
            elif self._answer is not None and message.get("id") == self._answer:
                return message

    async def settle(self, then: Callable[[], Awaitable[None]] | None = None) -> None:
        """Mark the end document once, never raising an ordinary error; then run `then` however it ends."""
        try:
            if not self._settled:
                self._settled = True
                try:
                    await self._finish()
                finally:
                    self._done()
        finally:
            if then is not None:
                await then()

    async def _finish(self) -> None:
        try:
            if self._cdp is None or self._listener is None:
                return
            answer, pages = await asyncio.wait_for(self._end_tree(self._cdp, self._listener), self._timeout)
            self._mark(_loader(((answer.get("result") or {}).get("frameTree") or {}).get("frame")))
            if pages - self._pages:
                self._mark(ANY_DOCUMENT)
                log.info("a page opened while an agent typed; saved-login capture makes no offer in this session")
        except BaseException as exc:
            self._fail(exc)
            if not isinstance(exc, Exception):
                raise
        finally:
            await self._close()

    async def _end_tree(self, cdp: CdpClient, listener: asyncio.Task[dict]) -> tuple[dict, frozenset[str]]:
        self._answer = cdp.next_request()
        await cdp.send("Page.getFrameTree")
        answer = await asyncio.shield(listener)
        return answer, await asyncio.to_thread(_page_ids, self._endpoint, self._timeout)

    def _fail(self, exc: BaseException) -> None:
        self._mark(ANY_DOCUMENT)
        log.info("saved-login capture couldn't follow the page an agent typed into (%s); no offer is made in this "
                 "session", type(exc).__name__)

    async def _close(self) -> None:
        listener, cdp, self._listener, self._cdp = self._listener, self._cdp, None, None
        if listener is not None:
            listener.cancel()
        if cdp is not None and getattr(cdp, "socket", None) is not None:
            try:
                await cdp.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001 - the marks are already set; no close error changes them
                log.info("saved-login capture's page watch didn't close cleanly (%s)", type(exc).__name__)

    async def __aenter__(self) -> AgentWrites:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.settle()


class NoWrites(AgentWrites):
    """A command that can't type, or one from the pane: nothing to mark."""

    def __init__(self) -> None:
        super().__init__(lambda _loader: None, 0)
        self._settled = True
