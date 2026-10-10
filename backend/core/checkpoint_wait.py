"""Exact-Turn control for a synchronous checkpoint waiter."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar


_T = TypeVar("_T")


@dataclass(slots=True)
class CheckpointWaitScope:
    """The one checkpoint this exact Turn synchronously started and awaits."""

    checkpoint_id: str | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)

    @property
    def stop_requested(self) -> bool:
        """Return whether Stop addressed this exact Turn scope."""
        return self._stop.is_set()

    def request_stop(self) -> None:
        """Release this Turn's waiter; the waiter decides the checkpoint's fate."""
        self._stop.set()

    async def wait_for_stop(self) -> None:
        """Return once Stop addresses this exact Turn scope."""
        await self._stop.wait()


async def await_checkpoint_or_stop(
    scope: CheckpointWaitScope,
    checkpoint_id: str,
    await_checkpoint: Callable[[str], Awaitable[_T]],
    on_stop: Callable[[CheckpointWaitScope], Awaitable[object]] | None = None,
) -> _T:
    """Await one checkpoint unless Stop releases the Turn.

    ``on_stop`` runs while the scope still names the awaited checkpoint, so
    its owner can end an attempt that is part of the stopped Turn's work.
    """
    scope.checkpoint_id = checkpoint_id
    checkpoint_waiter: asyncio.Future[_T] = asyncio.ensure_future(
        await_checkpoint(checkpoint_id)
    )
    stop_waiter = asyncio.create_task(scope._stop.wait())
    settled = False
    try:
        done, _pending = await asyncio.wait(
            (checkpoint_waiter, stop_waiter),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_waiter in done:
            raise asyncio.CancelledError
        result = checkpoint_waiter.result()
        settled = True
        return result
    finally:
        for waiter in (checkpoint_waiter, stop_waiter):
            if not waiter.done():
                waiter.cancel()
        await asyncio.gather(checkpoint_waiter, stop_waiter, return_exceptions=True)
        try:
            if (
                on_stop is not None
                and not settled
                and scope.stop_requested
                and scope.checkpoint_id == checkpoint_id
            ):
                await on_stop(scope)
        finally:
            if scope.checkpoint_id == checkpoint_id:
                scope.checkpoint_id = None
