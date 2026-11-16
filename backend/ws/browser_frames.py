"""A single pane writer with bounded visual backlog and lossless control messages."""
from __future__ import annotations

import asyncio
from collections import deque
import json
import time
from typing import Awaitable, Callable

from ws.browser_pane_stats import PaneStats


class PaneFrames:
    """Only a frame that has not started sending may be replaced by a newer frame."""

    _MAX_RELIABLE = 64

    def __init__(self, stats: PaneStats | None = None) -> None:
        self._stats = stats
        self._reliable: deque[str] = deque()
        self._frame: str | None = None
        self._ready = asyncio.Event()

    def offer(self, message: str) -> bool:
        started = time.perf_counter()
        try:
            parsed = json.loads(message)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict) and parsed.get("type") == "frame":
            replaced = self._frame is not None
            self._frame = message
            if self._stats is not None:
                self._stats.frame_offered(len(message), (time.perf_counter() - started) * 1000, replaced)
        elif len(self._reliable) >= self._MAX_RELIABLE:
            return False
        else:
            self._reliable.append(message)
        self._ready.set()
        return True

    def control(self, message: dict) -> bool:
        """Queue a reliable control message, reporting whether the bounded queue accepted it."""
        return self.offer(json.dumps(message, separators=(",", ":")))

    async def send(self, send_text: Callable[[str], Awaitable[None]]) -> None:
        while True:
            await self._ready.wait()
            if self._reliable:
                await send_text(self._reliable.popleft())
            elif self._frame is not None:
                message, self._frame = self._frame, None
                if self._stats is None:
                    await send_text(message)
                else:
                    message = self._stats.stamp(message)
                    started = time.perf_counter()
                    await send_text(message)
                    self._stats.frame_sent((time.perf_counter() - started) * 1000, len(message))
            else:
                self._ready.clear()
