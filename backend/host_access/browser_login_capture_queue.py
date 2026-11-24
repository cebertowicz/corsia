"""Serialize login-capture holder calls and invalidate work for ended sessions."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(eq=False)
class QueueItem:
    session_key: str
    ended: bool = False


tasks: set[asyncio.Task[None]] = set()
tails: dict[str, asyncio.Future[None]] = {}
items: dict[str, list[QueueItem]] = {}


def enqueue(thread_id, item, previous, finished, work, on_skip) -> None:
    async def run() -> None:
        try:
            if previous is not None:
                await asyncio.shield(previous)
            if item.ended:
                work.close()
                on_skip()
            else:
                await work
        finally:
            queued = items.get(thread_id, [])
            if item in queued:
                queued.remove(item)
            if not queued:
                items.pop(thread_id, None)
            if not finished.done():
                finished.set_result(None)
            if tails.get(thread_id) is finished:
                tails.pop(thread_id, None)
    task = asyncio.create_task(run())
    tasks.add(task)
    task.add_done_callback(tasks.discard)
