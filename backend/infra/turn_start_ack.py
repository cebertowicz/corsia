"""The native turn start handshake: ownership transfer without dead handoffs."""

from __future__ import annotations

import asyncio
import logging

from core.turn_core import TurnIdentity

log = logging.getLogger(__name__)


def settle_start_ack_on_completion(
    turn_task: asyncio.Task,
    start_ack: asyncio.Future,
) -> None:
    """Settle a pending start_ack when its turn task completes.

    A turn cancelled before its first step runs no handler, so nothing inside
    the turn can reject the handshake; completion-side settlement is the
    single owner of the "a dead turn settles its handoff" rule. The in-turn
    reject sites (failed admission, preemption, model-unavailable) remain
    early backstops that settle the same future sooner (#2261).
    """
    if start_ack.done():
        return

    def _settle(completed: asyncio.Task) -> None:
        if start_ack.done():
            return
        if completed.cancelled():
            start_ack.cancel()
            return
        failure = completed.exception()
        if failure is not None:
            start_ack.set_exception(failure)
            return
        start_ack.set_exception(RuntimeError(
            "native turn completed without settling start_ack"
        ))

    if turn_task.done():
        _settle(turn_task)
    else:
        turn_task.add_done_callback(_settle)


async def await_native_start(
    turn_task: asyncio.Task,
    start_ack: asyncio.Future,
) -> TurnIdentity:
    """Wait until a background native turn owns or rejects its durable start."""
    settle_start_ack_on_completion(turn_task, start_ack)
    try:
        identity = await asyncio.shield(start_ack)
    except asyncio.CancelledError:
        # Report cancellation only after the handshake settles. The settle
        # above bounds this re-await by turn-task completion, so a turn that
        # dies without resolving start_ack cannot strand the waiter (#2261).
        try:
            await asyncio.shield(start_ack)
        except asyncio.CancelledError:
            # A second cancellation of the waiter: the handshake stays
            # bounded by the turn task, and cancellation stays the
            # caller-visible outcome.
            pass
        except BaseException as start_failure:
            # The turn died before its handshake without any in-turn reject
            # running. Cancellation stays the caller-visible outcome, but
            # the turn's failure must not vanish silently.
            log.error(
                "native turn failed before its start handshake: %r",
                start_failure,
                exc_info=start_failure,
            )
        raise
    except BaseException:
        await turn_task
        raise
    if not isinstance(identity, TurnIdentity):
        raise RuntimeError("native turn acceptance did not return TurnIdentity")
    return identity
