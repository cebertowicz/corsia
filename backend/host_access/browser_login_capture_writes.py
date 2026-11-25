"""Mark every document an agent command that can type passes through (capture rule 7).

A second CDP connection to the agent's page sees each main-frame Page.frameNavigated while the command runs,
back/forward cache restores included, and a restored document keeps its loader id (checked on Chrome for
Testing 152). So the start document, each document the page shows and the end document are all marked,
whatever loads the command caused. A failure anywhere, cancel included, marks every document of the session.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from websockets.exceptions import WebSocketException

from host_access.browser_cdp import CdpClient
from host_access.browser_login_capture_cdp import quiet_cdp

log = logging.getLogger(__name__)
# Stands for "a document whose loader couldn't be read": the thread's every document counts as marked.
ANY_DOCUMENT = ""


def _loader(frame: object) -> str:
    loader = frame.get("loaderId") if isinstance(frame, dict) else None
    return loader if isinstance(loader, str) and loader else ANY_DOCUMENT


class AgentWrites:
    """One agent command's watch: start it before the command runs, settle it before the pane gets the page back."""

    def __init__(self, mark: Callable[[str], None], timeout: float) -> None:
        self._mark, self._timeout = mark, timeout
        self._cdp: CdpClient | None = None
        self._listener: asyncio.Task[dict] | None = None
        self._answer: int | None = None
        self._settled = False

    async def start(self, endpoint: str) -> None:
        """Mark the current document and watch for the next ones; on any failure mark every document."""
        try:
            self._cdp = quiet_cdp(endpoint)
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
                await self._finish()
        finally:
            if then is not None:
                await then()

    async def _finish(self) -> None:
        try:
            if self._cdp is None or self._listener is None:
                return
            answer = await asyncio.wait_for(self._end_tree(self._cdp, self._listener), self._timeout)
            self._mark(_loader(((answer.get("result") or {}).get("frameTree") or {}).get("frame")))
        except BaseException as exc:
            self._fail(exc)
            if not isinstance(exc, Exception):
                raise
        finally:
            await self._close()

    async def _end_tree(self, cdp: CdpClient, listener: asyncio.Task[dict]) -> dict:
        self._answer = cdp.next_request()
        await cdp.send("Page.getFrameTree")
        return await asyncio.shield(listener)

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
            except (OSError, WebSocketException) as exc:
                # The marks are already set; a socket that won't close cleanly changes none of them.
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
