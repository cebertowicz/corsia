"""Record a thread's Turn end for plan liveness and start the parents it woke."""

from __future__ import annotations

from loguru import logger


async def record_turn_end_logged(
    thread_id: str,
    arm_failure: str,
    drive_failure: str,
    *,
    level: str = "WARNING",
    **outcome,
) -> None:
    """Record a Turn end, deliver its invalidation, then drive woken parents.

    The notification owner contains recoverable preparation and delivery failure;
    write and parent-drive failures keep separate, truthful boundaries here.
    ``record_turn_end`` retries a lock refusal; any other write failure is logged.
    """
    try:
        from automation.plan_liveness_terminal import on_thread_turn_complete
        from automation.turn_end_effects import (
            deliver_turn_end_invalidations,
            record_turn_end,
        )
        from plans import public

        effects = await record_turn_end(
            on_thread_turn_complete,
            thread_id,
            **outcome,
        )
    except Exception as error:
        logger.log(level, f"{arm_failure}: {error}")
        return
    deliver_turn_end_invalidations(effects)
    try:
        await public.drive_threads(effects.drives)
    except Exception as error:
        logger.log(level, f"{drive_failure}: {error}")
