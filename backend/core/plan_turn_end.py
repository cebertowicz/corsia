"""Record a thread's Turn end for plan liveness and start the parents it woke."""

from __future__ import annotations

import dataclasses

from loguru import logger


async def record_turn_end_effects(
    thread_id: str, arm_failure: str, *, level: str = "WARNING", **outcome,
):
    """Record a Turn end and return its prepared effects, or None after logging a failure.

    ``record_turn_end`` retries a lock refusal; any other write failure is logged.
    """
    try:
        from automation.plan_liveness_terminal import on_thread_turn_complete
        from automation.turn_end_effects import record_turn_end

        return await record_turn_end(on_thread_turn_complete, thread_id, **outcome)
    except Exception as error:
        logger.log(level, f"{arm_failure}: {error}")
        return None


async def settlement_with_turn_end(settlement, thread_id: str, identity, error: str, db=None):
    """Attach the Turn-end record to the one APPLIED process-exit terminal.

    Process exits have no result event to record the Turn, so the first applied
    commit records it here. A failed record is logged and the settlement is
    returned unchanged, so its successor is still driven. A caller-supplied
    ``db`` (tests only) or a missing identity skips the record.
    """
    from infra.thread_driver import TerminalResult, TerminalSettlement

    if db is not None or identity is None:
        return settlement
    if not isinstance(settlement, TerminalSettlement) or settlement.result is not TerminalResult.APPLIED:
        return settlement
    effects = await record_turn_end_effects(
        thread_id, "plan-liveness after a process exit failed", level="ERROR",
        outcome="error", error=error, turn_id=identity.turn_id,
    )
    return settlement if effects is None else dataclasses.replace(settlement, turn_end_effects=effects)


async def ending_with_turn_end(ending, committed_failure, thread_id: str, identity, error: str, db=None):
    """Await a process-exit terminal and attach its Turn end, also when publishing failed after the commit.

    ``committed_failure`` is the harness's exception that carries the committed
    settlement past a failed publication; its settlement gets the record too.
    """
    try:
        settlement = await ending
    except committed_failure as failure:
        failure.settlement = await settlement_with_turn_end(failure.settlement, thread_id, identity, error, db)
        raise
    return await settlement_with_turn_end(settlement, thread_id, identity, error, db)


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
    """
    effects = await record_turn_end_effects(thread_id, arm_failure, level=level, **outcome)
    if effects is None:
        return
    from automation.turn_end_effects import deliver_turn_end_invalidations
    from plans import public

    deliver_turn_end_invalidations(effects)
    try:
        await public.drive_threads(effects.drives)
    except Exception as error:
        logger.log(level, f"{drive_failure}: {error}")
