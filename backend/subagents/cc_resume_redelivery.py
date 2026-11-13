"""Redeliver one input in its same Turn after Claude refused the native resume unread."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, cast

from core.turn_core import TurnIdentity
from subagents.cc_accept import accept_reconstructed_input
from subagents.cc_runtime_state import _LIVE, _Live

if TYPE_CHECKING:
    from core.checkpoint_wait import CheckpointWaitScope
    from subagents.cc_feed import _FeedAcceptance
    from subagents.cc_feed_transition import CCFeedTransition

DeferredFinish = Callable[[], Awaitable[None]]


async def redeliver_rejected_resume(
    transition: CCFeedTransition,
    live: _Live,
    identity: TurnIdentity,
    finish: DeferredFinish,
    acceptance: _FeedAcceptance,
    *,
    first_write_unproven: bool,
) -> TurnIdentity:
    """Deliver the same input in the same Turn after Claude refused it unread.

    A first write that never drained settled no delivery evidence, so this
    redelivery is the write that proves (or fails) the input's delivery.
    """
    if cast("CheckpointWaitScope", live.checkpoint_wait_scope).stop_requested:
        await finish()
        return identity
    try:
        replacement, frame, identity = await transition.rebuild_rejected_resume(live)
    except BaseException:
        if _LIVE.get(transition.thread_id) is live and live.corsia_turn == identity:
            await finish()
        raise
    return await accept_reconstructed_input(
        transition.thread_id, replacement, frame, turn_id=identity.turn_id,
        bcast=transition.bcast, preadmitted_turn=identity,
        input_message_id=acceptance.input_message_id,
        mark_accepted=acceptance.mark_accepted if first_write_unproven else None,
        mark_ambiguous=acceptance.mark_ambiguous if first_write_unproven else None,
        settle_delivery=acceptance.settle_delivery if first_write_unproven else None,
        settle_delivery_terminal=acceptance.settle_delivery_terminal if first_write_unproven else None,
    )
