"""Serialize login-capture holder calls and invalidate work for ended sessions."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(eq=False)
class QueueItem:
    session_key: str
    ended: bool = False


tasks: set[asyncio.Task[None]] = set()
_tails: dict[str, asyncio.Future[None]] = {}
_items: dict[str, list[QueueItem]] = {}


def begin(thread_id: str, session_key: str):
    item = QueueItem(session_key)
    _items.setdefault(thread_id, []).append(item)
    previous = _tails.get(thread_id)
    finished = asyncio.get_running_loop().create_future()
    _tails[thread_id] = finished
    return item, previous, finished


def latest_session(thread_id: str) -> str | None:
    return next((item.session_key for item in reversed(_items.get(thread_id, []))), None)


def end_session(thread_id: str, session_key: str | None) -> None:
    for item in _items.get(thread_id, []):
        if item.session_key == session_key:
            item.ended = True


def reset_for_tests() -> None:
    _items.clear()
    _tails.clear()


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
            queued = _items.get(thread_id, [])
            if item in queued:
                queued.remove(item)
            if not queued:
                _items.pop(thread_id, None)
            if not finished.done():
                finished.set_result(None)
            if _tails.get(thread_id) is finished:
                _tails.pop(thread_id, None)
    task = asyncio.create_task(run())
    tasks.add(task)
    task.add_done_callback(tasks.discard)
