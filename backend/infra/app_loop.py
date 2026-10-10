"""The one owner of the application event loop for work started off the loop.

``lifespan`` captures the running loop once at startup and clears it at
shutdown. ``submit`` hands a coroutine to that loop from any thread, a DB worker
included. Before startup or after shutdown it raises ``AppLoopUnavailable``
rather than queueing onto a closed loop. A submitted task is tracked, and its
exception is logged at ERROR, never lost in an unread future.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future
from collections.abc import Coroutine
from typing import Any, TypeVar

_LOOP: asyncio.AbstractEventLoop | None = None
_LOCK = threading.RLock()
_AWAITED: set[_Handoff] = set()
T = TypeVar("T")


class AppLoopUnavailable(RuntimeError):
    """No application event loop is running to take the work."""


def capture(loop: asyncio.AbstractEventLoop) -> None:
    """Record the application loop; called once by ``lifespan`` at startup."""
    global _LOOP
    with _LOCK:
        _LOOP = loop


def clear() -> None:
    """Forget the loop at shutdown so a late submit raises instead of queueing."""
    global _LOOP
    with _LOCK:
        _LOOP = None
        for receipt in tuple(_AWAITED):
            if receipt.task is None:
                receipt.refuse("application event loop cleared before starting")


def submit(coroutine: Coroutine[Any, Any, Any]) -> None:
    """Run ``coroutine`` as a tracked task on the application loop."""
    loop = _LOOP
    if loop is None or loop.is_closed():
        coroutine.close()
        raise AppLoopUnavailable("no application event loop is running")
    try:
        loop.call_soon_threadsafe(_start, coroutine)
    except RuntimeError as exc:
        # The loop closed between the check above and scheduling: the same
        # "no loop to take the work" condition, so callers see one exception type.
        coroutine.close()
        raise AppLoopUnavailable("application event loop closed while scheduling") from exc


def _start(coroutine: Coroutine[Any, Any, Any]) -> None:
    from ws.ingress import track_background_task

    track_background_task(asyncio.get_running_loop().create_task(coroutine))


class _Handoff:
    """One transient execution receipt; the caller cannot finish before its exact task."""

    def __init__(self, loop: asyncio.AbstractEventLoop, coroutine: Coroutine[Any, Any, Any]) -> None:
        self.loop = loop
        self.coroutine = coroutine
        self.result: Future = Future()
        self.task: asyncio.Task | None = None
        self.cancel_sent = False

    def refuse(self, reason: str) -> None:
        if self.result.done():
            return
        self.coroutine.close()
        _AWAITED.discard(self)
        self.result.set_exception(AppLoopUnavailable(reason))

    def start(self) -> None:
        from ws.ingress import track_background_task

        with _LOCK:
            if self.result.done():
                return
            if _LOOP is not self.loop or self.loop.is_closed():
                self.refuse("application event loop changed before starting")
                return
            self.task = self.loop.create_task(self.coroutine)
            self.task.add_done_callback(self.completed)
            track_background_task(self.task)

    def completed(self, task) -> None:
        with _LOCK:
            _AWAITED.discard(self)
            if task.cancelled():
                self.result.set_exception(asyncio.CancelledError())
            elif (error := task.exception()) is not None:
                self.result.set_exception(error)
            else:
                self.result.set_result(task.result())

    def cancel(self) -> None:
        with _LOCK:
            if self.result.done() or self.cancel_sent:
                return
            self.cancel_sent = True
            if self.task is None:
                self.coroutine.close()
                _AWAITED.discard(self)
                self.result.set_exception(asyncio.CancelledError())
            else:
                try:
                    current = asyncio.get_running_loop()
                except RuntimeError:
                    current = None
                if current is self.loop:
                    self.task.cancel()
                else:
                    self.loop.call_soon_threadsafe(self.task.cancel)


async def await_on_owner(coroutine: Coroutine[Any, Any, T], *, owner: asyncio.AbstractEventLoop | None) -> T:
    """Await one tracked application task, forwarding cancellation once and joining it.

    Only the canonical captured loop may own it. No DB worker waits on this API:
    async adapters call it before submitting their synchronous DB work.
    """
    with _LOCK:
        if owner is None or owner is not _LOOP or owner.is_closed() or not owner.is_running():
            coroutine.close()
            raise AppLoopUnavailable("no matching application event loop is running")
        receipt = _Handoff(owner, coroutine)
        _AWAITED.add(receipt)
        if asyncio.get_running_loop() is owner:
            # Own only this operation, never the surrounding native turn. The
            # independent task lets shutdown drain the same exact DB create.
            receipt.start()
        else:
            try:
                owner.call_soon_threadsafe(receipt.start)
            except RuntimeError:
                receipt.refuse("application event loop closed while scheduling")
        future = asyncio.wrap_future(receipt.result)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError as cancelled:
        receipt.cancel()
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if not future.cancelled():
            try:
                future.result()
            except BaseException:
                pass
        raise cancelled


def awaited_count() -> int:
    """Exact joined-handoff owners still preventing storage teardown."""
    with _LOCK:
        return len(_AWAITED)


async def drain_awaited() -> None:
    """After clear, cancel once and join only this owner's awaited executions."""
    with _LOCK:
        receipts = tuple(_AWAITED)
    for receipt in receipts:
        receipt.cancel()
    if receipts:
        from app_shutdown_join import join_cleanup

        _, pending_cancel = await join_cleanup(asyncio.wrap_future(receipt.result) for receipt in receipts)
        if pending_cancel is not None:
            raise pending_cancel
