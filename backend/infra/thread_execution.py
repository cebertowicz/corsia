"""Provider action executor for one claimed input batch."""

from __future__ import annotations

import asyncio
import uuid

from core.guidance_manifest import record_dispatch_manifest
from core.installed_context import persist_prepared_installed_context
from infra.db_async import run_in_db
from infra.failed_wake_surface import drive_failed_wake_owners
from infra.input_render import PreparedBatch
from infra.input_writes import ProviderWriteEvidence, dispatch_fenced_batch, settle_batch_write
from infra.plan_dispatch_events import report_terminal_unknown
from infra.queries import message_transcript_seq
from infra.send_broadcast import broadcast_claimed_batch, broadcast_newest_error
from infra.send_types import InboxQueuedInput, InputReceipt, SendResult
from infra.thread_input_types import ClaimedBatch, batch_option
from infra.time import utc_now_iso
from infra.turn_admission_driver import drive_turn_admission


async def _prepare_batch(batch: ClaimedBatch, *, thread_row: dict, target) -> PreparedBatch:
    """Patchable test seam over the canonical input renderer."""
    from infra.preflight_attempt import prepare_idle_prefix

    return await prepare_idle_prefix(batch, thread_row=thread_row, target=target)


async def _settle_accepted(attempt_id: str, evidence: ProviderWriteEvidence) -> None:
    evidence.mark_accepted()
    await run_in_db(settle_batch_write, attempt_id, outcome="accepted")


async def _bind_exact_turn(batch: ClaimedBatch, turn_id: str, _previous) -> None:
    if turn_id != batch.turn_id:
        raise RuntimeError("provider tried to replace the admitted Turn identity")


def _dispatch_context(
    send_dispatch,
    batch: ClaimedBatch,
    prepared: PreparedBatch,
    evidence: ProviderWriteEvidence,
    *,
    user: dict | None,
    ws,
    provider_acceptance,
    settle,
    settle_terminal,
):
    native_mode = str(batch_option(batch, "native_mode", "event"))
    if native_mode == "ws" and ws is None:
        native_mode = "event"
    return send_dispatch.DispatchContext(
        thread_id=batch.thread_id, thread_row=prepared.thread_row,
        content=prepared.content, is_subagent=prepared.is_subagent,
        target=prepared.target, turn_id=batch.turn_id,
        input_message_id=batch.anchor_message_id, input_ids=tuple(item.message_id for item in batch.items),

        metadata=batch.items[-1].metadata,
        native_mode=native_mode,
        allow_fresh=bool(
            prepared.provider == "codex"
            and not prepared.runtime_truth.get("codex_has_thread")
        ),
        user=user or {"id": str(prepared.thread_row.get("user_id") or "")},
        ws=ws,
        attachments=prepared.attachments,
        media_projection=prepared.media_projection,
        reasoning_effort=batch_option(batch, "reasoning_effort"),
        persist_input_once=_noop,
        settle_delivery=settle,
        bind_input_turn=lambda turn_id, previous: _bind_exact_turn(
            batch, turn_id, previous,
        ),
        mark_accepted=evidence.mark_accepted,
        mark_ambiguous=evidence.mark_ambiguous,
        prepared_context=prepared.prepared_context,
        refresh_prepared_context=prepared.refresh_prepared_context,
        preadmitted_turn=batch.identity,
        provider_acceptance=provider_acceptance,
        transfer_delivery_to_turn=evidence.transfer_to_turn,
        settle_delivery_terminal=settle_terminal,
    )


async def _dispatch_prepared(
    batch: ClaimedBatch,
    prepared: PreparedBatch,
    evidence: ProviderWriteEvidence,
    *,
    attempt_id: str,
    user: dict | None,
    ws,
    after_persist_before_dispatch,
    after_manifest_before_dispatch=None,
):
    import infra.send_dispatch as send_dispatch

    provider_acceptance = None
    if prepared.provider == "native":
        provider_acceptance = asyncio.get_running_loop().create_future()

    async def settle() -> None:
        if evidence.ambiguous:
            await run_in_db(
                settle_batch_write,
                attempt_id,
                outcome="ambiguous",
                error="provider write was observed but acceptance was not confirmed",
            )
            return
        await _settle_accepted(attempt_id, evidence)
        if evidence.turn_owned:
            await _broadcast_batch(batch)
        from infra.send_effects import run_batch_accepted_effects

        await run_batch_accepted_effects(batch, prepared.thread_row)

    async def settle_terminal(outcome: str, error: str | BaseException | None) -> None:
        if evidence.accepted or evidence.ambiguous or evidence.proved_no_write:
            return
        evidence.mark_proved_no_write()
        detail = error
        if outcome == "completed" and detail is None:
            detail = "Native Turn completed without provider-write acceptance"
        failed = await run_in_db(
            settle_batch_write, attempt_id, outcome="proved_no_write", error=detail,
            no_write_state="cancelled" if outcome == "cancelled" else "failed",
        )
        await _broadcast_batch(batch)
        await drive_failed_wake_owners(failed)

    if after_persist_before_dispatch is not None:
        await after_persist_before_dispatch(prepared.target)
    await run_in_db(
        persist_prepared_installed_context,
        batch.thread_id, prepared.prepared_context,
    )
    manifest_id = await run_in_db(record_dispatch_manifest, batch.thread_id, batch.turn_id, prepared)
    if after_manifest_before_dispatch is not None:  # refusing here leaves no provider write
        await after_manifest_before_dispatch(manifest_id)
    context = _dispatch_context(
        send_dispatch,
        batch,
        prepared,
        evidence,
        user=user,
        ws=ws,
        provider_acceptance=provider_acceptance,
        settle=settle,
        settle_terminal=settle_terminal,
    )
    return await send_dispatch.dispatch(prepared.provider, context)


async def _noop() -> None:
    return None


async def _broadcast_batch(batch: ClaimedBatch) -> None:
    """Keep the runtime patch seam over the canonical message broadcaster."""
    await broadcast_claimed_batch(batch)


async def execute_claimed_batch(
    batch: ClaimedBatch,
    *,
    thread_row: dict,
    target,
    user: dict | None = None,
    ws=None,
    dispatch_content: str | None = None,
    after_persist_before_dispatch=None,
    after_manifest_before_dispatch=None,
) -> SendResult | InboxQueuedInput:
    """Prepare and hand one claimed FIFO batch to its exact provider."""
    del dispatch_content
    await _broadcast_batch(batch)
    evidence = ProviderWriteEvidence()
    try:
        return await _execute_claimed_batch(
            batch,
            evidence=evidence,
            thread_row=dict(thread_row),
            target=target,
            user=user,
            ws=ws,
            after_persist_before_dispatch=after_persist_before_dispatch,
            after_manifest_before_dispatch=after_manifest_before_dispatch,
        )
    except BaseException as error:
        if evidence.turn_owned:
            raise
        from core.turn_core import settle_turn_terminal, terminal_error_detail

        outcome = "cancelled" if isinstance(error, asyncio.CancelledError) else "error"
        await settle_turn_terminal(
            batch.turn_id, outcome,
            terminal_error_detail(error) or None,
        )
        raise
    finally:
        if not evidence.turn_owned:
            await _broadcast_batch(batch)


async def _execute_claimed_batch(
    batch: ClaimedBatch,
    *,
    evidence: ProviderWriteEvidence,
    thread_row: dict,
    target,
    user: dict | None,
    ws,
    after_persist_before_dispatch,
    after_manifest_before_dispatch=None,
) -> SendResult | InboxQueuedInput:
    latest = batch.items[-1]
    prepared = await _prepare_batch(batch, thread_row=thread_row, target=target)
    # The claim's admitted load stays with a shortened prefix; its person's send remains queued behind it.
    batch = prepared.batch or batch
    attempt_id = str(uuid.uuid4())

    async def dispatcher(evidence):
        return await _dispatch_prepared(
            batch,
            prepared,
            evidence,
            attempt_id=attempt_id,
            user=user,
            ws=ws,
            after_persist_before_dispatch=after_persist_before_dispatch,
            after_manifest_before_dispatch=after_manifest_before_dispatch,
        )

    driver, turn_task, identity = await dispatch_fenced_batch(
        batch,
        dispatcher,
        attempt_id=attempt_id,
        started_at=utc_now_iso(),
        evidence=evidence,
    )
    if identity != batch.identity:
        raise RuntimeError("provider accepted a different Turn identity")
    if latest.message_id != batch.items[-1].message_id:
        return InboxQueuedInput(InputReceipt(
            batch.thread_id, latest.message_id, latest.input_order,
            latest.revision, latest.created_at,
        ))
    seq = await run_in_db(
        message_transcript_seq, batch.thread_id, latest.message_id,
    )
    return SendResult(
        driver=driver,
        message_id=latest.message_id,
        created_at=latest.created_at,
        seq=seq,
        turn_id=identity.turn_id,
        turn_order=identity.turn_order,
        turn_task=turn_task,
    )


def _load_thread(thread_id: str) -> dict:
    from infra.database import get_db

    connection = get_db()
    try:
        row = connection.execute(
            "SELECT * FROM threads WHERE id=?", (thread_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"task does not exist: {thread_id}")
        return dict(row)
    finally:
        connection.close()


async def drive_claimed_batch(
    batch: ClaimedBatch,
) -> SendResult | InboxQueuedInput:
    """Execute runnable work returned by the transition transaction."""
    from infra.unavailable_model_recovery import resolve_or_hold

    try:
        thread_row = await run_in_db(_load_thread, batch.thread_id)
        resolved = await resolve_or_hold(batch.target_model, batch)
    except BaseException as error:
        from core.turn_core import settle_turn_terminal, terminal_error_detail

        outcome = "cancelled" if isinstance(error, asyncio.CancelledError) else "error"
        await settle_turn_terminal(
            batch.turn_id, outcome,
            terminal_error_detail(error) or None,
        )
        raise
    if isinstance(resolved, InboxQueuedInput):
        return resolved
    return await execute_claimed_batch(
        batch,
        thread_row=thread_row,
        target=resolved,
        user={"id": str(thread_row.get("user_id") or "")},
    )


async def drive_thread(
    thread_id: str,
) -> SendResult | InboxQueuedInput | None:
    """Start one idle FIFO head or request the active Turn's safe boundary."""
    from infra.model_switches import advance_pending_switch, read_switch

    pending = await run_in_db(read_switch, thread_id)
    if pending is not None and pending.waiting_for_source:
        from thread.model_runtime import broadcast_switch_inputs

        pending = await run_in_db(advance_pending_switch, thread_id)
        await broadcast_switch_inputs(pending)
    if pending is not None and not pending.waiting_for_source:
        from thread.model_pending import drive_pending_model_switch

        await drive_pending_model_switch(thread_id)
        return None
    result = await drive_idle_thread(thread_id)
    if result is not None:
        return result
    await _request_active_boundary(thread_id)
    return None


async def drive_idle_thread(
    thread_id: str, *, connection_limit_subject=None,
) -> SendResult | InboxQueuedInput | None:
    """Start one idle FIFO head without changing a running Turn's boundary."""
    from core.turn_core import BOOT_EPOCH
    from infra.thread_inputs import start_idle_turn

    admission = await run_in_db(
        start_idle_turn,
        thread_id,
        turn_id=str(uuid.uuid4()),
        boot_epoch=BOOT_EPOCH,
        started_at=utc_now_iso(),
        **({'connection_limit_subject': connection_limit_subject} if connection_limit_subject is not None else {}),
    )
    if admission is not None:
        return await drive_turn_admission(admission)
    return None


async def _request_active_boundary(thread_id: str) -> None:
    from infra.database import get_db
    from infra.send_service import request_safe_boundary
    from llm.resolution import resolve_execution_target

    connection = get_db()
    try:
        row = connection.execute(
            "SELECT model FROM active_turns WHERE thread_id=? AND status='running'",
            (thread_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return
    target = await run_in_db(resolve_execution_target, str(row["model"]))
    await request_safe_boundary(thread_id, target.harness)


async def drive_terminal_settlement(settlement) -> None:
    """Drive every task made runnable by one committed terminal transition."""
    from infra.connection_limit_drive import record_probe_outcome
    record_probe_outcome(settlement.thread_id, settlement.probe_outcome)
    await report_terminal_unknown(settlement)
    if settlement.idle_checkpoint is not None:
        from thread.idle_checkpoint import arm_idle_checkpoint

        arm_idle_checkpoint(settlement.idle_checkpoint)
    if settlement.switch_transition is not None:
        from thread.model_runtime import apply_switch_transition

        await apply_switch_transition(settlement.switch_transition)
    if settlement.switch_request is not None:
        from thread.model_pending import drive_pending_model_switch
        from thread.model_runtime import broadcast_switch_inputs

        await broadcast_switch_inputs(settlement.switch_request)
        await drive_pending_model_switch(settlement.thread_id)
    if settlement.successor is not None:
        await drive_turn_admission(settlement.successor)
    elif settlement.probe_outcome in ("error", "limited") and settlement.thread_id:
        # The live error frame was sent while the Turn still ran ('failed');
        # an input left queued behind it now makes it 'held'.
        await broadcast_newest_error(settlement.thread_id)
    for thread_id in settlement.wake_thread_ids:
        await drive_thread(thread_id)
    await drive_failed_wake_owners(settlement.failed_message_ids)
