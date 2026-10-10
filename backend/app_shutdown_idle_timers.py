"""Shut down idle-checkpoint timers only once no owner can still arm one.

Native and harness teardown can still complete a Turn and arm a fresh timer,
so this runs after both rather than in the earlier owned-task batch, which
would snapshot the registry before they quiesce and leave any timer armed
during their teardown unjoined.
"""

from __future__ import annotations

import asyncio

import infra.idle_checkpoint_timers as idle_checkpoint_timers
from app_shutdown_join import join_cleanup


async def shutdown_idle_checkpoint_timers(logger) -> asyncio.CancelledError | None:
    """Cancel and join every idle-checkpoint timer still in the registry."""
    tasks = idle_checkpoint_timers.cancel_all()
    results, pending_cancel = await join_cleanup(tasks)
    for result in results:
        if isinstance(result, BaseException) and not isinstance(
            result, asyncio.CancelledError,
        ):
            logger.warning(f"shutdown: idle checkpoint timer cleanup failed: {result}")
    return pending_cancel


async def fold_idle_checkpoint_shutdown(
    logger, pending_cancel: asyncio.CancelledError | None,
) -> asyncio.CancelledError | None:
    """Run the idle-timer shutdown step and fold its result into the caller's."""
    idle_timer_cancel = await shutdown_idle_checkpoint_timers(logger)
    return pending_cancel or idle_timer_cancel


def remaining_idle_timers() -> tuple[str, ...]:
    """Thread ids whose idle timer is still registered after shutdown ran."""
    return tuple(idle_checkpoint_timers.pending())
