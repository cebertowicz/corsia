"""Ordered input and overflow handling for the shared browser pane."""
import asyncio
import collections
import contextlib
import json
from typing import AsyncIterator

from fastapi import WebSocket, WebSocketDisconnect


async def pane_messages(pane: WebSocket) -> AsyncIterator[dict]:
    """The pane's messages in the order sent, less each pointer move a later queued move replaces.

    Chrome answers a pointer move only after its next frame, tens of milliseconds, while the pane
    sends one per animation frame; delivered one at a time, moves pile up and a click behind them
    lands seconds late. So the pane is read while earlier messages are delivered, and a move that
    arrives right behind a queued move replaces it, so a stalled page never holds more than one
    waiting move. Nothing else is dropped or reordered, and the pane's disconnect is raised only
    after everything it sent first. A page that answers no input while the backlog reaches
    `_MAX_QUEUED_INPUT` is stalled: the pane is closed, as an overflowing output path closes it.
    """
    received: collections.deque[dict | Exception] = collections.deque()
    arrived = asyncio.Event()

    reader = asyncio.create_task(_receive_pane_messages(pane, received, arrived))
    try:
        while True:
            await arrived.wait()
            arrived.clear()
            while received:
                data = received.popleft()
                if isinstance(data, Exception):
                    raise data
                yield data
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


async def _receive_pane_messages(pane: WebSocket, received: collections.deque[dict | Exception],
                                 arrived: asyncio.Event) -> None:
    try:
        while True:
            try:
                data = json.loads(await pane.receive_text())
            except (TypeError, ValueError):
                continue
            if isinstance(data, dict):
                if _is_move(data) and received and _is_move(received[-1]):
                    received[-1] = data
                elif len(received) >= _MAX_QUEUED_INPUT:
                    received.clear()
                    with contextlib.suppress(Exception):
                        await pane.close(code=1013)
                    raise WebSocketDisconnect(code=1013)
                else:
                    received.append(data)
                arrived.set()
    except Exception as exc:
        received.append(exc)
        arrived.set()


# The pane sends at most one wheel event per animation frame, so this is about 17 s of input
# against a page that has answered none of it.
_MAX_QUEUED_INPUT = 1024


def _is_move(data: dict | Exception) -> bool:
    return isinstance(data, dict) and data.get("type") == "input_mouse" and data.get("eventType") == "mouseMoved"


async def close_on_overflow(pane: WebSocket, overflow: asyncio.Event) -> None:
    """Close the pane once any reliable output path exhausts its bounded queue."""
    await overflow.wait()
    with contextlib.suppress(Exception):
        await pane.close(code=1013)
