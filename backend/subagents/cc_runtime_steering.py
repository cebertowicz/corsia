"""Claude Code true-steer adapter over the canonical message FIFO."""

from __future__ import annotations

import asyncio
import logging
import uuid

from infra.db_async import run_in_db
from infra.input_render import prepare_live_input, projected_input_tokens
from infra.input_writes import (
    begin_batch_write,
    release_batch_after_proved_no_write,
    settle_batch_write,
)
from infra.queries import load_thread_row
from infra.thread_inputs import ClaimedBatch, claim_safe_boundary
from infra.time import utc_now_iso
from subagents.cc_runtime_state import _LIVE

log = logging.getLogger(__name__)



async def _broadcast(batch: ClaimedBatch) -> None:
    from infra.send_broadcast import broadcast_message_rows

    await broadcast_message_rows(
        batch.thread_id,
        tuple(item.message_id for item in batch.items),
    )


async def _release_unwritten(batch: ClaimedBatch) -> None:
    from infra.input_writes import release_batch_before_write

    await run_in_db(release_batch_before_write, batch)
    await _broadcast(batch)


async def _release_after_failure(batch: ClaimedBatch) -> None:
    """Return an unwritten claim without ever replacing the caller's error."""
    try:
        await _release_unwritten(batch)
    except Exception as error:
        log.warning(
            "cc live inbox release failed thread_id=%s cause=%s: %s",
            batch.thread_id, type(error).__name__, error,
        )


async def _feed_claimed(batch: ClaimedBatch, live, thread_row: dict, target) -> bool:
    # Preparation owns its release: on failure, and for an unfitting suffix.
    prepared = await prepare_live_input(batch, live, thread_row, target)
    if prepared is None:
        await _broadcast(batch)
        return False
    if len(prepared[0].items) != len(batch.items):
        await _broadcast(batch)
    batch, rendered = prepared
    attempt_id = str(uuid.uuid4())
    try:
        await run_in_db(
            begin_batch_write,
            batch,
            attempt_id=attempt_id,
            started_at=utc_now_iso(),
        )
    except BaseException:
        await _release_after_failure(batch)
        raise
    media = rendered.media_projection
    observed = False
    accepted = False

    def on_write() -> None:
        nonlocal observed
        observed = True

    def on_accepted() -> None:
        nonlocal accepted
        accepted = True

    try:
        await live.session.feed(
            rendered.content,
            on_write_observed=on_write,
            on_accepted=on_accepted,
            input_context=(
                batch.anchor_message_id, projected_input_tokens(rendered, target),
            ),
            **({"media_projection": media} if media is not None else {}),
        )
    except BaseException as error:
        proved_no_write = await _settle_failure(batch, attempt_id, observed, error)
        if proved_no_write and not isinstance(error, asyncio.CancelledError):
            return False
        raise  # Stop must still reach the tool task that holds the reply.
    if not accepted:
        log.warning(
            "cc live inbox delivery declined thread_id=%s "
            "reason=provider_declined accepted=%r",
            batch.thread_id, accepted,
        )
        rejection = RuntimeError("Claude Code did not acknowledge steer delivery")
        proved_no_write = await _settle_failure(
            batch, attempt_id, observed, rejection,
        )
        if proved_no_write:
            return False
        raise rejection
    await run_in_db(settle_batch_write, attempt_id, outcome="accepted")
    await _broadcast(batch)
    from infra.send_effects import run_batch_accepted_effects

    await run_batch_accepted_effects(batch, thread_row)
    return True


async def _settle_failure(
    batch: ClaimedBatch,
    attempt_id: str,
    observed: bool,
    error: BaseException,
) -> bool:
    if observed:
        await run_in_db(
            settle_batch_write,
            attempt_id,
            outcome="ambiguous",
            error=str(error),
        )
        await _broadcast(batch)
        return False
    await run_in_db(
        release_batch_after_proved_no_write,
        batch,
        attempt_id=attempt_id,
        error=None,
    )
    await _broadcast(batch)
    return True


async def deliver_live_inbox_head(thread_id: str) -> bool:
    """Steer the current CC Turn only from the task holding its last tool reply.

    The CLI reads queued stdin at a tool boundary or after a result. Written
    while the final reply is still held, the steer is read before the next
    model call; written at any other time, its answer may follow the Turn's
    result and be dropped (#2204). Everything else waits for the successor Turn.
    """
    from llm.resolution import resolve_execution_target

    async with _LIVE.lock(thread_id):
        live = _LIVE.get(thread_id)
        identity = live.corsia_turn if live is not None else None
        if live is None:
            log.debug(
                "cc live inbox delivery declined thread_id=%s reason=no_live_handle",
                thread_id,
            )
            return False
        if identity is None:
            log.debug(
                "cc live inbox delivery declined thread_id=%s reason=no_corsia_turn",
                thread_id,
            )
            return False
        if not live.turn_active:
            log.debug(
                "cc live inbox delivery declined thread_id=%s reason=no_running_turn",
                thread_id,
            )
            return False
        if live.session is None:
            log.debug(
                "cc live inbox delivery declined thread_id=%s reason=no_session",
                thread_id,
            )
            return False
        held = getattr(live, "admitted_tool_tasks", None) or set()
        if live.tool_step_ids or held != {asyncio.current_task()}:
            log.debug(
                "cc live inbox delivery declined thread_id=%s "
                "reason=not_last_held_reply steps=%d tasks=%d",
                thread_id, len(live.tool_step_ids), len(held),
            )
            return False
        batch = await run_in_db(
            claim_safe_boundary, thread_id, identity.turn_id,
        )
        if batch is None:
            return False
        if batch.target_model != live.catalog_model_id:
            log.warning(
                "cc live inbox delivery declined thread_id=%s "
                "reason=model_mismatch batch_model=%s live_model=%s",
                thread_id, batch.target_model, live.catalog_model_id,
            )
            await _release_unwritten(batch)
            return False
        await _broadcast(batch)
        try:
            thread_row = await run_in_db(load_thread_row, thread_id)
            target = await run_in_db(resolve_execution_target, batch.target_model)
        except BaseException:
            await _release_after_failure(batch)
            raise
        return await _feed_claimed(batch, live, thread_row, target)


async def steer_held_reply(thread_id: str) -> None:
    """Steer from the reply-holding tool task without failing its tool.

    A failure before the write starts leaves the input queued for the
    successor Turn. A failure after it starts, including one that also fails
    to settle the write, is settled ambiguous at the latest by the Turn's
    terminal. Stop propagates.
    """
    try:
        await deliver_live_inbox_head(thread_id)
    except Exception as error:
        log.warning(
            "cc held-reply steer failed thread_id=%s cause=%s: %s",
            thread_id, type(error).__name__, error,
        )
