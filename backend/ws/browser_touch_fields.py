"""Report text-field geometry only to a pane that declares a touch viewer."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

from host_access.browser_session_state import BrowserSessionError

log = logging.getLogger(__name__)
_SETTLE_SECONDS = 0.25


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

    def frame(self, raw: str) -> None:
        if not self.touch:
            return
        try:
            message = json.loads(raw)
            metadata = message.get('metadata', {})
            width, height = metadata.get('deviceWidth'), metadata.get('deviceHeight')
            if message.get('type') == 'frame' and isinstance(width, (int, float)) and isinstance(height, (int, float)) and width > 0 and height > 0:
                self.size = (width, height)
                self.generation += 1
                self.changed.set()
        except (ValueError, TypeError, AttributeError):
            return

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
            while True:
                self.changed.clear()
                try:
                    await asyncio.wait_for(self.changed.wait(), timeout=_SETTLE_SECONDS)
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
