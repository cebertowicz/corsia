"""Fold one producer-owned completed-Turn marker through its source adapter.

Every idle fold is warm or skipped, never cold. External harnesses (Claude
Code, Codex) fork their captured provider prefix; a native Turn forks the
final call's input its terminal handed to the marker (``native_receipt``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from core.checkpoint_types import (
    CheckpointRecord,
    CheckpointRequest,
    IdleCheckpointAdmissionResult,
    IdleCheckpointReason,
    IdleCheckpointSkip,
)
from infra.checkpoint_admission import IDLE_MIN_DELTA_TOKENS, admits_idle_checkpoint
from infra.db_async import run_in_db
from infra.idle_checkpoints import IdleCheckpointDue

if TYPE_CHECKING:
    from core.checkpoint_native_admission import NativeIdleReceipt
    from core.checkpoint_resume import CheckpointAdmission


@dataclass(frozen=True, slots=True)
class IdleCheckpointContext:
    """One still-pending Turn's frozen checkpoint inputs."""

    due: IdleCheckpointDue
    target: Any
    boundary: Any
    previous: CheckpointRecord | None
    working_dir: str
    reasoning_effort: str | None
    binary: str | None


@dataclass(frozen=True, slots=True)
class IdleCheckpointPreparation:
    """Frozen checkpoint provenance awaiting fallible provider preparation."""

    due: IdleCheckpointDue
    context: IdleCheckpointContext
    request: CheckpointRequest


def _resolved_target(db, model: str):
    from llm.resolution import ModelUnavailableError, resolve_execution_target

    try:
        target = resolve_execution_target(model, db=db)
    except ModelUnavailableError:
        return None
    if not target.available or not target.context_window:
        return None
    return target


def _warm_refused(cause: str) -> IdleCheckpointSkip:
    return IdleCheckpointSkip(IdleCheckpointReason.WARM_CAPTURE_REFUSED, cause=cause)


def _native_receipt_boundary(
    due: IdleCheckpointDue, projection, previous: CheckpointRecord | None,
) -> Any:
    """The due Turn's end when it is the receipt's answer; never the sealed end.

    Passive rows after the Turn were never in the prefix, so they stay for the
    next fold. The idle gate still measures to the sealed end; accepted, since
    the two coincide: passive events stay out of the projection until their
    own Turn runs, and a Turn starting clears this marker.

    A missing receipt (restart, errored or unmeasured final call), a checkpoint
    installed since the receipt's input was built, or any other end refuses.
    """
    receipt = due.native_receipt
    if receipt is None:
        return _warm_refused("no_receipt")
    if receipt.generation_id != (previous.id if previous is not None else None):
        return _warm_refused("base_changed")
    for turn in projection.turns:
        if turn.turn_id == due.turn_id and turn.includes_turn_end:
            if turn.boundary.message_id == receipt.final_message_id:
                return turn.boundary
    return _warm_refused("boundary_mismatch")


def read_idle_context(
    due: IdleCheckpointDue,
    *,
    db=None,
) -> IdleCheckpointContext | IdleCheckpointSkip:
    """Freeze one marker's current source facts without consuming the marker."""
    from core.checkpoint_fit import foldable_tail_tokens
    from core.checkpoint_render import checkpoint_binary
    from history.projection import render_portable_history
    from history.types import HistoryBoundary
    from infra.checkpoint_queries import active_for_thread, current_installed
    from infra.database import get_db
    from llm.reasoning_config import effective_reasoning_effort
    from subagents.codex_runtime_support import resolve_working_dir

    connection = db or get_db()
    try:
        if db is None:
            connection.execute("BEGIN")
        thread_row = connection.execute(
            "SELECT s.*,t.model AS idle_source_model FROM active_turns AS t "
            "JOIN threads AS s ON s.id=t.thread_id "
            "WHERE t.id=? AND t.thread_id=? AND t.status='completed' "
            "AND t.idle_checkpoint_pending=1",
            (due.turn_id, due.thread_id),
        ).fetchone()
        if thread_row is None or not thread_row["model"]:
            return IdleCheckpointSkip(IdleCheckpointReason.STALE)
        source_model = str(thread_row["idle_source_model"])
        if source_model != str(thread_row["model"]):
            return IdleCheckpointSkip(IdleCheckpointReason.STALE)
        if active_for_thread(due.thread_id, db=connection) is not None:
            return IdleCheckpointSkip(IdleCheckpointReason.TURN_RUNNING)
        target = _resolved_target(connection, source_model)
        if target is None:
            return IdleCheckpointSkip(IdleCheckpointReason.TARGET_UNAVAILABLE)
        previous = current_installed(due.thread_id, db=connection)
        previous_boundary = (
            HistoryBoundary.of_checkpoint(previous)
            if previous is not None
            else None
        )
        projection = render_portable_history(
            due.thread_id, db=connection, start_exclusive=previous_boundary,
        )
        boundary = projection.sealed_boundary
        if boundary is None:
            return IdleCheckpointSkip(IdleCheckpointReason.NO_BOUNDARY)
        foldable_tokens = foldable_tail_tokens(
            projection, previous_boundary,
            model=target.expected_reported_model or target.transport_model,
        )
        if not admits_idle_checkpoint(foldable_tokens):
            return IdleCheckpointSkip(
                IdleCheckpointReason.BELOW_MINIMUM,
                foldable_tokens=foldable_tokens,
                threshold_tokens=IDLE_MIN_DELTA_TOKENS,
            )
        if target.harness == "native":
            native = _native_receipt_boundary(due, projection, previous)
            if isinstance(native, IdleCheckpointSkip):
                return native
            boundary = native
        return IdleCheckpointContext(
            due=due,
            target=target,
            boundary=boundary,
            previous=previous,
            working_dir=resolve_working_dir(thread_row["project_id"], connection),
            reasoning_effort=effective_reasoning_effort(
                thread_row["reasoning_effort"], target.catalog_id
            ),
            binary=checkpoint_binary(target, connection),
        )
    finally:
        if db is None:
            connection.close()


async def prepare_idle_checkpoint(
    due: IdleCheckpointDue,
    checkpoint_id: str,
) -> IdleCheckpointPreparation | IdleCheckpointSkip:
    """Freeze one eligible marker's provenance before provider preparation."""
    from infra.thread_inputs import TurnIdentity

    context = await run_in_db(read_idle_context, due)
    if isinstance(context, IdleCheckpointSkip):
        return context
    from core.checkpoint_warm_request import build_warm_request

    request = build_warm_request(
        checkpoint_id=checkpoint_id,
        thread_id=due.thread_id,
        turn=TurnIdentity(due.turn_id, due.turn_order),
        trigger="idle",
        target=context.target,
        boundary=context.boundary,
        previous_generation_id=context.previous.id if context.previous else None,
    )
    return IdleCheckpointPreparation(due, context, request)


async def complete_idle_checkpoint(
    preparation: IdleCheckpointPreparation,
) -> CheckpointAdmission | IdleCheckpointSkip:
    """Capture the same-source handle for one frozen idle request."""
    from core.checkpoint_render import capture_warm_checkpoint_source
    from core.checkpoint_warm_request import complete_warm_admission

    context = preparation.context
    if context.target.harness == "native":
        return await _complete_native_idle_checkpoint(preparation)
    warm_source = await capture_warm_checkpoint_source(
        preparation.due.thread_id,
        context.boundary,
        target=context.target,
        binary=context.binary,
        reasoning_effort=context.reasoning_effort,
    )
    if warm_source is None:
        return IdleCheckpointSkip(IdleCheckpointReason.WARM_CAPTURE_REFUSED)
    return complete_warm_admission(
        preparation.request,
        target=context.target,
        working_dir=context.working_dir,
        reasoning_effort=context.reasoning_effort,
        binary=context.binary,
        warm_source=warm_source,
    )


async def _complete_native_idle_checkpoint(
    preparation: IdleCheckpointPreparation,
) -> CheckpointAdmission | IdleCheckpointSkip:
    """Fork the receipt's live prefix warm, or skip before any row exists."""
    from core.checkpoint_native_fork import capture_live_warm_source, warm_input_fits
    from core.checkpoint_warm_request import complete_warm_admission

    context = preparation.context
    # read_idle_context already refused a marker without a receipt.
    receipt = cast("NativeIdleReceipt", preparation.due.native_receipt)
    source = capture_live_warm_source(
        preparation.due.thread_id,
        context.boundary,
        messages=list(receipt.messages),
        tools=list(receipt.tools),
        reasoning_effort=context.reasoning_effort,
        preserve_thinking=context.target.preserve_thinking_default,
        provider_input_tokens=receipt.provider_input_tokens,
        rendered_input_tokens=receipt.rendered_input_tokens,
    )
    admission = complete_warm_admission(
        preparation.request,
        target=context.target,
        working_dir=context.working_dir,
        reasoning_effort=context.reasoning_effort,
        binary=context.binary,
        warm_source=source,
    )
    if not await warm_input_fits(admission.provider_request):
        return _warm_refused("input_too_large")
    return admission


def _emit_result(
    due: IdleCheckpointDue,
    result: IdleCheckpointAdmissionResult,
    skip: IdleCheckpointSkip | None = None,
) -> None:
    from core.checkpoint_types import IdleCheckpointDisposition
    from infra.product_events import emit_product_event
    from loguru import logger

    if result.disposition is IdleCheckpointDisposition.ADMITTED:
        record = result.record
        logger.info(
            "idle_checkpoint admitted: thread={} turn={} through_seq={}",
            due.thread_id, due.turn_id,
            record.through_seq if record is not None else None,
        )
        emit_product_event(
            "idle_checkpoint",
            "started",
            thread_id=due.thread_id,
            properties={
                "reason": result.reason.value,
                "through_seq": record.through_seq if record is not None else None,
                "writer_harness": record.writer_harness if record is not None else None,
            },
        )
        return
    reason = skip.reason if skip is not None else result.reason
    properties: dict[str, object] = {"reason": reason.value}
    if skip is not None and skip.reason is IdleCheckpointReason.BELOW_MINIMUM:
        properties["foldable_tokens"] = skip.foldable_tokens
        properties["threshold_tokens"] = skip.threshold_tokens
    if skip is not None and skip.cause is not None:
        properties["cause"] = skip.cause
    logger.info(
        "idle_checkpoint not_admitted: thread={} turn={} reason={} cause={} disposition={} "
        "foldable_tokens={} threshold_tokens={}",
        due.thread_id, due.turn_id, reason.value, properties.get("cause"),
        result.disposition.value,
        properties.get("foldable_tokens"), properties.get("threshold_tokens"),
    )
    emit_product_event(
        "idle_checkpoint", "refused", thread_id=due.thread_id, properties=properties,
    )


async def _record_failure(
    due: IdleCheckpointDue,
    preparation: IdleCheckpointPreparation | None,
    error: Exception,
) -> None:
    from core.checkpoint_runtime import fail_idle_checkpoint
    from infra.product_events import emit_product_event
    from loguru import logger

    logger.opt(exception=error).error("Idle checkpoint failed for {}", due.thread_id)
    try:
        await fail_idle_checkpoint(
            due.turn_id,
            preparation.request if preparation is not None else None,
            error,
        )
    except Exception:
        logger.exception("Idle checkpoint failure commit failed for {}", due.thread_id)
    emit_product_event(
        "idle_checkpoint",
        "failed",
        thread_id=due.thread_id,
        properties={"error": type(error).__name__},
    )


async def fire_idle_checkpoint(due: IdleCheckpointDue) -> None:
    """Measure one marker and request its fold, consuming it either way."""
    from core.checkpoint_runtime import request_idle_checkpoint

    preparation: IdleCheckpointPreparation | None = None
    skip: IdleCheckpointSkip | None = None
    try:
        prepared = await prepare_idle_checkpoint(due, str(uuid4()))
        if isinstance(prepared, IdleCheckpointSkip):
            skip = prepared
        else:
            preparation = prepared
        admission = None
        if preparation is not None:
            captured = await complete_idle_checkpoint(preparation)
            if isinstance(captured, IdleCheckpointSkip):
                skip = captured
            else:
                admission = captured
        result = await request_idle_checkpoint(due.turn_id, admission)
    except Exception as error:
        await _record_failure(due, preparation, error)
        return
    _emit_result(due, result, skip)


def arm_idle_checkpoint(due: IdleCheckpointDue) -> None:
    """Arm the thread's one timer to fold ``due`` after the idle delay."""
    import infra.idle_checkpoint_timers as idle_checkpoint_timers
    from infra.time import utc_now

    delay = (due.fires_at() - utc_now()).total_seconds()
    idle_checkpoint_timers.arm(due, delay, fire_idle_checkpoint)


async def rearm_idle_checkpoints() -> int:
    """At boot, re-arm still-warm markers; expired ones were cleared unfolded."""
    from infra.idle_checkpoints import recover_idle_markers
    from infra.time import utc_now

    live = await run_in_db(recover_idle_markers, now=utc_now())
    for due in live:
        arm_idle_checkpoint(due)
    return len(live)
