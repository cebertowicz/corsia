"""Ordered input and overflow handling for the shared browser pane."""
import asyncio
import collections
import contextlib
import json
import math
from typing import AsyncIterator

from fastapi import WebSocket, WebSocketDisconnect

from ws.browser_pane_stats import PaneStats


async def pane_messages(pane: WebSocket, stats: PaneStats | None = None) -> AsyncIterator[dict]:
    """Read ahead while input is delivered, combining compatible queued pointer and wheel events.

    Chrome may answer continuous input more slowly than the pane sends it. Adjacent pointer moves
    replace one another, and adjacent wheels at the same point and in the same direction add their
    deltas. Direction changes and other input remain ordered barriers. The pane's disconnect is raised
    only after everything it sent first. A page that answers no input while the backlog reaches
    `_MAX_QUEUED_INPUT` is stalled: the pane is closed, as an overflowing output path closes it.
    """
    received: collections.deque[dict | Exception] = collections.deque()
    arrived = asyncio.Event()

    reader = asyncio.create_task(_receive_pane_messages(pane, received, arrived, stats))
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
                                 arrived: asyncio.Event, stats: PaneStats | None = None) -> None:
    try:
        while True:
            try:
                data = json.loads(await pane.receive_text())
            except (TypeError, ValueError):
                continue
            if isinstance(data, dict):
                if _is_move(data) and received and _is_move(received[-1]):
                    received[-1] = data
                    if stats is not None:
                        stats.input_coalesced()
                elif received and (merged := _merge_wheels(received[-1], data)) is not None:
                    received[-1] = merged
                    if stats is not None:
                        stats.input_coalesced()
                elif len(received) >= _MAX_QUEUED_INPUT:
                    received.clear()
                    with contextlib.suppress(Exception):
                        await pane.close(code=1013)
                    raise WebSocketDisconnect(code=1013)
                else:
                    received.append(data)
                    if stats is not None:
                        stats.backlog(len(received))
                arrived.set()
    except Exception as exc:
        received.append(exc)
        arrived.set()


# The pane sends at most one wheel event per animation frame, so this is about 17 s of input
# against a page that has answered none of it.
_MAX_QUEUED_INPUT = 1024


def _is_move(data: dict | Exception) -> bool:
    return isinstance(data, dict) and data.get("type") == "input_mouse" and data.get("eventType") == "mouseMoved"


def _merge_wheels(previous: dict | Exception, current: dict) -> dict | None:
    """Sum adjacent wheels aimed at the same point, without crossing a reversal or other input."""
    if not isinstance(previous, dict) or any(
        item.get('type') != 'input_mouse' or item.get('eventType') != 'mouseWheel'
        for item in (previous, current)
    ):
        return None
    ignored = {'deltaX', 'deltaY', 'id'}
    if {k: v for k, v in previous.items() if k not in ignored} != {
        k: v for k, v in current.items() if k not in ignored
    }:
        return None
    totals = {}
    for axis in ('deltaX', 'deltaY'):
        a, b = previous.get(axis), current.get(axis)
        if (not isinstance(a, (int, float)) or isinstance(a, bool)
                or not isinstance(b, (int, float)) or isinstance(b, bool)):
            return None
        try:
            if not all(math.isfinite(v) for v in (a, b, a + b)) or (a < 0 < b) or (b < 0 < a):
                return None
        except OverflowError:
            return None
        totals[axis] = a + b
    return {**current, **totals}


async def close_on_overflow(pane: WebSocket, overflow: asyncio.Event) -> None:
    """Close the pane once any reliable output path exhausts its bounded queue."""
    await overflow.wait()
    with contextlib.suppress(Exception):
        await pane.close(code=1013)
