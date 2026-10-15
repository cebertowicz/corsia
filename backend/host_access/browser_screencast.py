"""The browser pane's own screencast of the shared page."""
from __future__ import annotations

import json
from typing import Callable

import websockets

from host_access.browser_cdp import CdpClient
from host_access.browser_session_state import BrowserSessionError


class PaneScreencast:
    """One pane's own screencast of the shared page target.

    agent-browser's stream can stop sending frames after a viewport change while Chrome keeps
    drawing, so each pane reads Chrome's frames itself. Chrome sends the next frame only once the
    last is acknowledged, so every frame is acknowledged on arrival, before it is relayed. After a
    page-metrics change Chrome can drop the next changes' frames; a restart sends a fresh frame and
    resumes them, so the command path restarts each pane's screencast after such a command.

    This session captures the window's whole view. Chrome will not make a window narrower than its
    minimum, so a page emulated narrower than that sits at the frame's left with the page's overflow
    beside it. Each start measures the page's layout viewport first, and frames report that size,
    so the pane draws and maps input to the page alone. While a metrics command runs, and until
    the following start's measurement answers, frames may be at a size that no longer holds, such as
    a screenshot's temporary capture size: they are acknowledged but not shown, and the start's own
    frame follows at once.
    """

    QUALITY = 80

    def __init__(self, endpoint: str, deliver: Callable[[str], object]) -> None:
        self.endpoint = endpoint
        self._deliver = deliver
        self._size: tuple[int, int] | None = None
        self._page: tuple[int, int] | None = None
        self._unmeasured = 0  # Measurements sent and not yet answered.
        self._held = False
        self._cdp: CdpClient | None = None
        self._starts: set[int] = set()
        self._measures: set[int] = set()

    async def run(self) -> None:
        """Relay frames until the pane leaves or Chrome closes the page target."""
        # A high-density frame can exceed the request client's 1 MiB message cap.
        async with CdpClient(self.endpoint, max_size=None) as cdp:
            self._cdp = cdp
            try:
                await self._start(cdp)
                async for raw in cdp.socket:
                    message = json.loads(raw)
                    if message.get("method") == "Page.screencastFrame":
                        await self._relay_frame(cdp, message.get("params"))
                    elif (answered := message.get("id")) in self._starts | self._measures:
                        if "error" in message:
                            raise BrowserSessionError("This page couldn't be shown")
                        if answered in self._measures:
                            self._measures.discard(answered)
                            self._measured(message.get("result"))
                        self._starts.discard(answered)
            finally:
                self._cdp = None

    def capture_at(self, width: int, height: int) -> None:
        """Capture at most this many device pixels from the next start on."""
        self._size = (width, height)

    def hold(self) -> None:
        """Show no frame from now until the next start's measurement: the page's metrics are changing."""
        self._held = True

    async def restart(self) -> None:
        """Restart capture, which makes Chrome send the current picture at once."""
        if (cdp := self._cdp) is None:
            return  # run() has not started capturing yet, or has ended.
        try:
            await cdp.send("Page.stopScreencast")
            await self._start(cdp)
        except websockets.ConnectionClosed:
            pass  # The pane is leaving; run() ends with the closed socket.

    async def _start(self, cdp: CdpClient) -> None:
        # Chrome answers in order, so the measurement arrives before this start's first frame.
        # Recorded before sending: the reader may take the answer before send returns.
        self._unmeasured += 1
        self._held = False  # The measurement now holds frames until it answers.
        self._measures.add(cdp.next_request())
        await cdp.send("Page.getLayoutMetrics")
        params: dict[str, object] = {"format": "jpeg", "quality": self.QUALITY}
        if self._size is not None:
            params.update(maxWidth=self._size[0], maxHeight=self._size[1])
        self._starts.add(cdp.next_request())
        await cdp.send("Page.startScreencast", **params)

    def _measured(self, result: object) -> None:
        viewport = result.get("cssLayoutViewport") if isinstance(result, dict) else None
        width = viewport.get("clientWidth") if isinstance(viewport, dict) else None
        height = viewport.get("clientHeight") if isinstance(viewport, dict) else None
        if not isinstance(width, int) or not isinstance(height, int):
            raise BrowserSessionError("This page couldn't be shown")
        self._page = (width, height)
        self._unmeasured -= 1

    async def _relay_frame(self, cdp: CdpClient, params: object) -> None:
        session = params.get("sessionId") if isinstance(params, dict) else None
        if not isinstance(params, dict) or not isinstance(session, int):
            # Without its session id the frame cannot be acknowledged, and Chrome would send no other.
            raise BrowserSessionError("This page couldn't be shown")
        await cdp.send("Page.screencastFrameAck", sessionId=session)
        if self._held or self._unmeasured:
            return
        data, metadata = params.get("data"), params.get("metadata")
        if isinstance(data, str) and isinstance(metadata, dict):
            width, height = metadata.get("deviceWidth"), metadata.get("deviceHeight")
            if self._page is not None and isinstance(width, (int, float)) and isinstance(height, (int, float)):
                metadata = {**metadata, "deviceWidth": min(width, self._page[0]), "deviceHeight": min(height, self._page[1])}
            self._deliver(json.dumps({"type": "frame", "data": data, "metadata": metadata}, separators=(",", ":")))
