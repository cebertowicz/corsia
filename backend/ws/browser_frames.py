"""A single pane writer with bounded visual backlog and lossless control messages."""
from __future__ import annotations

import asyncio
from collections import deque
import json
from typing import Awaitable, Callable


class PaneFrames:
    """Only a frame that has not started sending may be replaced by a newer frame."""

    def __init__(self) -> None:
        self._reliable: deque[str] = deque()
        self._frame: str | None = None
        self._ready = asyncio.Event()

    def offer(self, message: str) -> None:
        try:
            parsed = json.loads(message)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict) and parsed.get("type") == "frame":
            self._frame = message
        else:
            self._reliable.append(message)
        self._ready.set()

    def control(self, message: dict) -> None:
        self.offer(json.dumps(message, separators=(",", ":")))

    async def send(self, send_text: Callable[[str], Awaitable[None]]) -> None:
        while True:
            await self._ready.wait()
            if self._reliable:
                await send_text(self._reliable.popleft())
            elif self._frame is not None:
                message, self._frame = self._frame, None
                await send_text(message)
            else:
                self._ready.clear()
