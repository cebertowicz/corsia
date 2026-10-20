"""Codex true-steer adapter over the canonical message FIFO."""

from __future__ import annotations

import logging
import uuid

from infra.db_async import run_in_db
from infra.input_render import prepare_live_input, projected_input_tokens
from infra.input_writes import (
    begin_batch_write,
    release_batch_after_proved_no_write,
    release_batch_before_write,
    settle_batch_write,
)
from infra.queries import load_thread_row
from infra.thread_inputs import ClaimedBatch, claim_safe_boundary
from infra.time import utc_now_iso
from subagents.codex_runtime_event_delivery import (
    arm_delivery_event_barrier as _begin_event_buffer,
    release_delivery_event_barrier as _release_event_buffer,
)
from subagents.codex_runtime_state import _LIVE

log = logging.getLogger(__name__)


async def _broadcast(batch: ClaimedBatch) -> None:
    from infra.send_broadcast import broadcast_message_rows

    await broadcast_message_rows(
        batch.thread_id,
        tuple(item.message_id for item in batch.items),
    )


def _provider_rejected(error: BaseException) -> bool:
    from subagents.codex_app_server_errors import CodexAppServerError

    return isinstance(error, CodexAppServerError) and "code" in error.payload



async def _settle_failure(
    batch: ClaimedBatch,
    attempt_id: str,
    write_observed: bool,
    error: BaseException,
) -> bool:
    if write_observed and not _provider_rejected(error):
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


async def _steer_claimed(batch: ClaimedBatch, live, thread_row: dict, target) -> bool:
    prepared = await prepare_live_input(batch, live, thread_row, target)
    if prepared is None:
        await _broadcast(batch)
        return False
    if len(prepared[0].items) != len(batch.items):
        await _broadcast(batch)
    batch, rendered = prepared
    media = rendered.media_projection
    attempt_id = str(uuid.uuid4())
    write_observed = False

    def on_write() -> None:
        nonlocal write_observed
        write_observed = True

    await run_in_db(
        begin_batch_write,
        batch,
        attempt_id=attempt_id,
        started_at=utc_now_iso(),
    )
    _begin_event_buffer(live)
    try:
        accepted = await live.session.steer(
            rendered.content,
            on_write_observed=on_write,
            on_drain_confirmed=lambda: None,
            input_context=(
                batch.anchor_message_id, projected_input_tokens(rendered, target),
            ),
            **({"media_projection": media} if media is not None else {}),
        )
        if accepted is not True:
            log.warning(
                "codex live inbox delivery declined thread_id=%s "
                "reason=provider_declined accepted=%r",
                batch.thread_id, accepted,
            )
            error = RuntimeError("Codex declined the current safe boundary")
            await _settle_failure(batch, attempt_id, False, error)
            return False
        await run_in_db(settle_batch_write, attempt_id, outcome="accepted")
        await _broadcast(batch)
    except BaseException as error:
        proved_no_write = await _settle_failure(
            batch, attempt_id, write_observed, error,
        )
        if proved_no_write:
            return False
        raise
    finally:
        await _release_event_buffer(live)
    try:
        from infra.send_effects import run_batch_accepted_effects

        await run_batch_accepted_effects(batch, thread_row)
    except Exception:
        log.exception("Codex accepted effects failed for %s", batch.thread_id)
    return True


async def deliver_live_inbox_batch(
    thread_id: str,
) -> bool:
    """Steer one compatible FIFO prefix into the exact current Codex Turn."""
    from functools import partial

    from llm.locality import LOCALITY_REFUSALS
    from llm.resolution import resolve_thread_target
    from subagents.codex_runtime import settle_dead_live_generation_if_observed

    dead_live = False
    async with _LIVE.lock(thread_id):
        live = _LIVE.get(thread_id)
        identity = live.corsia_turn if live is not None else None
        if live is not None and getattr(live.session, "returncode", None) is not None:
            dead_live = True
            log.debug(
                "codex live inbox delivery declined thread_id=%s "
                "reason=dead_live_session returncode=%r",
                thread_id, live.session.returncode,
            )
        elif live is None:
            log.debug(
                "codex live inbox delivery declined thread_id=%s reason=no_live_handle",
                thread_id,
            )
            return False
        elif identity is None:
            log.debug(
                "codex live inbox delivery declined thread_id=%s reason=no_corsia_turn",
                thread_id,
            )
            return False
        elif live.session is None:
            log.debug(
                "codex live inbox delivery declined thread_id=%s reason=no_session",
                thread_id,
            )
            return False
        elif tool_step_ids := getattr(live, "tool_step_ids", {}):
            log.debug(
                "codex live inbox delivery declined thread_id=%s "
                "reason=outstanding_tool_steps count=%d",
                thread_id, len(tool_step_ids),
            )
            return False
        else:
            batch = await run_in_db(
                claim_safe_boundary, thread_id, identity.turn_id,
            )
            if batch is None:
                return False
            if batch.target_model != live.catalog_model_id:
                log.warning(
                    "codex live inbox delivery declined thread_id=%s "
                    "reason=model_mismatch batch_model=%s live_model=%s",
                    thread_id, batch.target_model, live.catalog_model_id,
                )
                await run_in_db(release_batch_before_write, batch)
                await _broadcast(batch)
                return False
            await _broadcast(batch)
            try:
                thread_row = await run_in_db(load_thread_row, thread_id)
                target = await run_in_db(partial(resolve_thread_target, batch.target_model, thread_id=thread_id))
            except BaseException:
                await run_in_db(release_batch_before_write, batch)
                await _broadcast(batch)
                raise
            if target.availability_reason in LOCALITY_REFUSALS:
                # Policy now forbids the model: the successor Turn's admission holds the input.
                await run_in_db(release_batch_before_write, batch)
                await _broadcast(batch)
                return False
            return await _steer_claimed(batch, live, thread_row, target)
    if dead_live:
        await settle_dead_live_generation_if_observed(thread_id)
    return False
