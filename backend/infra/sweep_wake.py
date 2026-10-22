"""Interrupt the continuation loop's sleep so it re-reads the next row deadline."""

from __future__ import annotations

import asyncio

_loop: asyncio.AbstractEventLoop | None = None
_event: asyncio.Event | None = None


def attach() -> asyncio.Event:
    """Bind the running loop; returns the event the sweep loop waits on."""
    global _loop, _event
    _loop, _event = asyncio.get_running_loop(), asyncio.Event()
    return _event


def sweep_soon() -> None:
    """Wake the sweep loop to recompute its next deadline; safe from any thread."""
    loop, event = _loop, _event
    if loop is not None and event is not None and not loop.is_closed():
        loop.call_soon_threadsafe(event.set)
