"""The sole in-memory registry of per-thread idle checkpoint timers.

A thread has at most one pending timer. The durable marker in
``active_turns.idle_checkpoint_pending`` stays the truth; a timer only decides
when that marker is measured. Cancellation is called from database write
paths that may run on ``run_in_db`` worker threads, so every mutation is
locked and task cancellation is marshalled onto the timer's own loop.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from loguru import logger

from infra.idle_checkpoints import IdleCheckpointDue

# The per-thread timer replaced the sweep's rate limit (3 starts per 15
# minutes) with a concurrency ceiling instead: at most this many warm folds
# run at once, process-wide, regardless of how many threads go idle at the
# same moment.
IDLE_CONCURRENT_LIMIT = 3


@dataclass(frozen=True, slots=True)
class _Timer:
    due: IdleCheckpointDue
    loop: asyncio.AbstractEventLoop
    task: asyncio.Task


_LOCK = threading.Lock()
_TIMERS: dict[str, _Timer] = {}
# Released timers still preparing or requesting their fold; only shutdown
# cancels these.
_FIRING: set[asyncio.Task] = set()
_FOLD_SLOTS = asyncio.Semaphore(IDLE_CONCURRENT_LIMIT)


def _cancel(timer: _Timer) -> None:
    if timer.loop.is_closed():
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is timer.loop:
        timer.task.cancel()
    else:
        timer.loop.call_soon_threadsafe(timer.task.cancel)


def _release(thread_id: str, task: asyncio.Task) -> bool:
    """Drop this exact timer before it folds; False when it was superseded."""
    with _LOCK:
        current = _TIMERS.get(thread_id)
        if current is None or current.task is not task:
            return False
        del _TIMERS[thread_id]
        _FIRING.add(task)
        return True


async def _wait_then_fire(
    due: IdleCheckpointDue,
    delay: float,
    fire: Callable[[IdleCheckpointDue], Awaitable[None]],
) -> None:
    await asyncio.sleep(max(delay, 0.0))
    # A queued timer is still armed and cancellable while it waits for a
    # fold slot, so a fresh input can still invalidate it here instead of
    # wasting a slot on a marker that no longer applies.
    async with _FOLD_SLOTS:
        task = asyncio.current_task()
        # Once released, input invalidation can no longer interrupt an
        # admitted fold; the marker's atomic consume decides whether it
        # still applies.
        if task is None or not _release(due.thread_id, task):
            logger.info(
                "idle_checkpoint timer_superseded: thread={} turn={}",
                due.thread_id, due.turn_id,
            )
            return
        logger.info(
            "idle_checkpoint timer_fired: thread={} turn={}",
            due.thread_id, due.turn_id,
        )
        try:
            await fire(due)
        finally:
            with _LOCK:
                _FIRING.discard(task)


def arm(
    due: IdleCheckpointDue,
    delay: float,
    fire: Callable[[IdleCheckpointDue], Awaitable[None]],
) -> None:
    """Replace the thread's timer with one that folds ``due`` after ``delay``."""
    loop = asyncio.get_running_loop()
    task = loop.create_task(
        _wait_then_fire(due, delay, fire),
        name=f"idle-checkpoint:{due.thread_id}",
    )
    with _LOCK:
        previous = _TIMERS.get(due.thread_id)
        _TIMERS[due.thread_id] = _Timer(due, loop, task)
    logger.info(
        "idle_checkpoint armed: thread={} turn={} delay={:.1f}s replaced={}",
        due.thread_id, due.turn_id, delay, previous is not None,
    )
    if previous is not None:
        _cancel(previous)


def cancel(thread_id: str) -> None:
    """Cancel the thread's pending timer, if any, from any thread."""
    with _LOCK:
        timer = _TIMERS.pop(thread_id, None)
    if timer is not None:
        logger.info(
            "idle_checkpoint timer_cancelled: thread={} turn={}",
            thread_id, timer.due.turn_id,
        )
        _cancel(timer)


def cancel_all() -> tuple[asyncio.Task, ...]:
    """Cancel every pending and firing timer; return only this loop's own.

    Cancellation itself works across loops through ``_cancel``'s thread-safe
    path, so any timer whose loop is still open is cancelled here regardless
    of which loop is calling. A loop that has already closed (for example a
    test's own ``asyncio.run()`` that exited without resolving its timer)
    cannot be touched at all; that entry is logged and dropped instead.
    Returning it, or any task on a different but still-open loop, to the
    caller's own ``asyncio.gather`` would raise, since a loop can only await
    its own tasks -- so only the tasks that belong to the loop currently
    running this call are returned.
    """
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    with _LOCK:
        timers = list(_TIMERS.values())
        firing = tuple(_FIRING)
        _TIMERS.clear()
        _FIRING.clear()
    live_timers: list[_Timer] = []
    for timer in timers:
        if timer.loop.is_closed():
            logger.warning(
                "Dropped idle-checkpoint timer for a closed loop: thread={}",
                timer.due.thread_id,
            )
            continue
        live_timers.append(timer)
        _cancel(timer)
    live_firing: list[asyncio.Task] = []
    for task in firing:
        loop = task.get_loop()
        if loop.is_closed():
            logger.warning(
                "Dropped firing idle-checkpoint task for a closed loop: {}",
                task.get_name(),
            )
            continue
        live_firing.append(task)
        if loop is running:
            task.cancel()
        else:
            loop.call_soon_threadsafe(task.cancel)
    return (
        *(timer.task for timer in live_timers if timer.loop is running),
        *(task for task in live_firing if task.get_loop() is running),
    )


def pending() -> dict[str, IdleCheckpointDue]:
    """Snapshot the armed markers by thread."""
    with _LOCK:
        return {thread_id: timer.due for thread_id, timer in _TIMERS.items()}
