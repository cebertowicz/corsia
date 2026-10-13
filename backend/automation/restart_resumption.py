"""Detached continuation scheduling after a restart hold is released."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Iterable
from typing import Any

from loguru import logger


def resume_threads_detached(
    thread_ids: Iterable[str],
    drive_thread: Callable[[str], Coroutine[Any, Any, object]],
    tasks: set[asyncio.Task],
) -> None:
    """Resume continued threads without holding the release caller open."""
    for thread_id in thread_ids:
        task: asyncio.Task[object] = asyncio.create_task(drive_thread(thread_id))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        task.add_done_callback(_log_resume_failure)


def _log_resume_failure(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    failure = task.exception()
    if failure is not None:
        logger.opt(exception=failure).error("restart resume failed after abort")
