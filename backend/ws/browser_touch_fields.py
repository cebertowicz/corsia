"""Report text-field geometry only to a pane that declares a touch viewer."""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from websockets.exceptions import WebSocketException

from host_access.browser_session_state import BrowserSessionError
from host_access.processes import ProcessOwnerError

log = logging.getLogger(__name__)
# The read waits for the pane's frames to pause: Chrome sends a frame only when the page draws, so a
# still page sending nothing is the only sign it stopped. Measured with the product's own PaneScreencast
# at the phone capture size on Chrome for Testing 152 (headless, own /tmp profile, 4 s windows,
# 2026-11-23): while a page animates or wheel-scrolls, frames come 27-32 ms apart (p95 148-150 ms, worst
# 747 ms), and a page that stops sending none at all. So the quiet window must clear an ordinary hitch:
# 0.25s is past the p95 of those gaps, and only a rare long stall (that 747 ms one, mid-scroll) reads as a
# pause; the next pause corrects it, because a read only ever reports boxes. A page that draws forever
# never pauses, so it is read at this ceiling instead: 2s, about three times the worst measured hitch,
# which is how long its boxes can go stale and costs one measured read (10-50 ms, see browser_text_fields)
# per period.
_SETTLE_SECONDS = 0.25
_MAX_SETTLE_SECONDS = 2.0


class TouchFields:
    def __init__(self, thread_id: str, read: Callable[[float, float], Awaitable[tuple[list[dict], tuple[object, object] | None]]], report: Callable[[dict], None]):
        self.thread_id = thread_id
        self.read = read
        self.report = report
        self.touch = False
        self.size: tuple[float, float] | None = None
        self.changed = asyncio.Event()
        self.last: list[dict] | None = None
        self.focused: tuple[object, object] | None = None
        self.generation = 0

    def appearance(self, message: dict) -> None:
        self.touch = message.get('touch') is True
        width, height = message.get('width'), message.get('height')
        if self.touch and isinstance(width, (int, float)) and isinstance(height, (int, float)) and width > 0 and height > 0:
            self.size = (width, height)
            self.generation += 1
            self.changed.set()
        elif not self.touch:
            self.size = None
            self.last = None
            self.focused = None
            self.generation += 1

    def frame(self, _raw: str) -> None:
        if self.touch:
            self.changed.set()

    def pointer_event(self, event_type: object) -> None:
        if event_type == 'mouseWheel':
            self.reset()
        elif self.touch and event_type == 'mousePressed':
            # A new person tap can focus a different field without being a page-initiated blur.
            self.focused = None
            self.generation += 1
            self.changed.set()

    def reset(self) -> None:
        if self.touch:
            if self.last != []:
                self.last = []
                self.report({'type': 'text_fields', 'fields': []})
            self.focused = None
            self.generation += 1
            self.changed.set()

    async def run(self) -> None:
        while True:
            await self.changed.wait()
            deadline = asyncio.get_running_loop().time() + _MAX_SETTLE_SECONDS
            while True:
                self.changed.clear()
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                # Not asyncio.wait_for: on Python 3.11 it returns normally when a frame lands in the same loop
                # pass as a cancel, so a closing pane's watcher would never stop (#3209).
                try:
                    async with asyncio.timeout(min(_SETTLE_SECONDS, remaining)):
                        await self.changed.wait()
                except TimeoutError:
                    break
            if not self.touch or self.size is None:
                continue
            generation = self.generation
            try:
                fields, focused = await self.read(*self.size)
            except (BrowserSessionError, ProcessOwnerError, OSError, WebSocketException, TimeoutError):
                # A failed read tells nothing about the page: keep the last state and report nothing,
                # so a dropped read never closes the phone keyboard while someone is typing.
                log.warning('browser pane %s could not read text-field boxes', self.thread_id, exc_info=True)
                continue
            if not self.touch or generation != self.generation:
                continue
            if self.focused is not None and focused != self.focused:
                self.report({'type': 'text_field_blurred'})
            self.focused = focused
            if fields != self.last:
                self.last = fields
                self.report({'type': 'text_fields', 'fields': fields})
