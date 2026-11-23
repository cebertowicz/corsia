"""Report text-field geometry only to a pane that declares a touch viewer."""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from host_access.browser_session_state import BrowserSessionError

log = logging.getLogger(__name__)
# browser_dom_write checks moving pages every 0.05s for up to 40 reads (2s).
# Five intervals give a quiet window; the same 40-interval budget caps animation delay.
_SETTLE_SECONDS = 5 * 0.05
_MAX_SETTLE_SECONDS = 40 * 0.05


class TouchFields:
    def __init__(self, read: Callable[[float, float], Awaitable[tuple[list[dict], tuple[object, object] | None]]], report: Callable[[dict], None]):
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
                try:
                    await asyncio.wait_for(self.changed.wait(), timeout=min(_SETTLE_SECONDS, remaining))
                except TimeoutError:
                    break
            if not self.touch or self.size is None:
                continue
            generation = self.generation
            try:
                fields, focused = await self.read(*self.size)
            except (BrowserSessionError, OSError, TimeoutError):
                log.warning('Could not read browser text-field boxes')
                fields, focused = [], None
            if not self.touch or generation != self.generation:
                continue
            if self.focused is not None and focused != self.focused:
                self.report({'type': 'text_field_blurred'})
            self.focused = focused
            if fields != self.last:
                self.last = fields
                self.report({'type': 'text_fields', 'fields': fields})
