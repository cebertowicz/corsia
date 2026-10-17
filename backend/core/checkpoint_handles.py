"""This process's live checkpoint handles: the one record of which attempts it owns."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from core.checkpoint_types import CheckpointRecord


@dataclass(slots=True)
class Handle:
    task: asyncio.Task[CheckpointRecord]
    cancel_event: asyncio.Event
    thread_id: str
    tool_executor: Any = None


tasks: dict[str, Handle] = {}
# Ids whose running row may commit before ``tasks`` holds their handle.
launching: dict[str, asyncio.Event] = {}


@contextmanager
def launch_window(checkpoint_id: str | None) -> Iterator[None]:
    """Own an id from before its row commits until its handle is registered.

    Without this, a waiter that reads the committed running row before the
    launch registers its handle would see no owner and settle a live attempt.
    """
    if checkpoint_id is None:
        yield
        return
    ready = launching[checkpoint_id] = asyncio.Event()
    try:
        yield
    finally:
        launching.pop(checkpoint_id, None)
        ready.set()


def forget(checkpoint_id: str, task: asyncio.Task[CheckpointRecord]) -> None:
    """Drop a finished task's handle and consume its exception."""
    handle = tasks.get(checkpoint_id)
    if handle is not None and handle.task is task:
        tasks.pop(checkpoint_id, None)
    if not task.cancelled():
        task.exception()
