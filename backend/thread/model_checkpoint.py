"""Build the one warm or target-cold checkpoint for a pending model switch."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import cast
from uuid import uuid4

from core.checkpoint_resume import CheckpointAdmission
from core.checkpoint_types import CheckpointRecord, CheckpointSummary
from infra.model_switch_types import SwitchRequest
from llm.reasoning_config import effective_reasoning_effort


@dataclass(frozen=True, slots=True)
class SwitchCheckpointContext:
    """Frozen source, destination, and history facts for one admission attempt."""

    request: SwitchRequest
    source_target: object | None
    destination_target: object
    boundary: object
    previous: CheckpointRecord | None
    projection: object
    working_dir: str
    reasoning_effort: str | None
    source_binary: str | None
    destination_binary: str | None
    destination_mcp_config: str | None


def _current_cc_config(thread_id: str) -> str | None:
    from subagents.cc_runtime_state import _LIVE

    live = _LIVE.get(thread_id)
    session = getattr(live, "session", None)
    value = getattr(session, "mcp_config", None) or getattr(live, "tool_config", None)
    return str(value) if isinstance(value, str) and value else None


def _switch_thread_row(db, request: SwitchRequest):
    thread_row = db.execute(
        "SELECT * FROM threads WHERE id=?", (request.thread_id,),
    ).fetchone()
    if (
        thread_row is None
        or str(thread_row["model"] or "") != request.source_model
        or str(thread_row["pending_model"] or "") != request.destination_model
        or str(thread_row["switch_operation_id"] or "") != request.operation_id
    ):
        raise RuntimeError("pending model switch changed before checkpoint admission")
    return thread_row


def _switch_targets(db, request: SwitchRequest):
    """Resolve both ends through the thread's resolver; a source its policy forbids is no source."""
    from llm.locality import LOCALITY_REFUSALS
    from llm.resolution import ModelUnavailableError, resolve_thread_target

    try:
        source = resolve_thread_target(request.source_model, thread_id=request.thread_id, db=db)
    except (ModelUnavailableError, ValueError):
        source = None
    if source is not None and source.availability_reason in LOCALITY_REFUSALS:
        source = None
    destination = resolve_thread_target(request.destination_model, thread_id=request.thread_id, db=db)
    if not destination.available or not destination.context_window:
        raise RuntimeError(
            destination.availability_reason or "switch destination is unavailable"
        )
    return source, destination


def read_switch_checkpoint_context(
    request: SwitchRequest,
) -> SwitchCheckpointContext:
    """Freeze the latest sealed history without changing source or switch state."""
    from core.checkpoint_render import checkpoint_binary
    from history.projection import render_portable_history
    from history.types import HistoryBoundary
    from infra.checkpoint_queries import current_installed
    from infra.database import get_db
    from subagents.codex_runtime_support import resolve_working_dir

    db = get_db()
    try:
        db.execute("BEGIN")
        thread_row = _switch_thread_row(db, request)
        source, destination = _switch_targets(db, request)
        previous = current_installed(request.thread_id, db=db)
        projection = render_portable_history(
            request.thread_id, db=db,
            start_exclusive=HistoryBoundary.of_checkpoint(previous) if previous else None,
        )
        if projection.sealed_boundary is None:
            raise RuntimeError("model switch has no sealed history to checkpoint")
        return SwitchCheckpointContext(
            request,
            source,
            destination,
            projection.sealed_boundary,
            previous,
            projection,
            resolve_working_dir(thread_row["project_id"], db),
            effective_reasoning_effort(thread_row["reasoning_effort"], destination.catalog_id),
            checkpoint_binary(source, db),
            checkpoint_binary(destination, db),
            _current_cc_config(request.thread_id)
            if destination.harness == "claude-code"
            else None,
        )
    finally:
        db.close()


async def prepare_source_checkpoint(
    request: SwitchRequest,
    checkpoint_id: str,
) -> CheckpointAdmission | None:
    """Prepare one source-authored warm fold, or report no warm source."""
    if request.checkpoint_choice == "target":
        return None
    from core.checkpoint_render import capture_warm_checkpoint_source
    from core.checkpoint_warm_request import build_warm_admission, warm_capture_refused
    from infra.db_async import run_in_db

    context = await run_in_db(read_switch_checkpoint_context, request)
    if context.source_target is None:
        # No resolved source target means its harness is unknown too.
        return warm_capture_refused(request.thread_id, "unknown", "source_unresolved")
    if (
        context.source_target.harness == "native"
        and context.source_target.provider == "omlx"
    ):
        from llm.capabilities import make_provider_params
        from llm.providers.omlx_lifecycle import (
            OmlxLoadedStateError,
            require_loaded_model,
        )

        provider = await run_in_db(
            make_provider_params,
            context.source_target.transport_model,
        )
        try:
            await require_loaded_model(
                provider.get("api_base"),
                provider.get("model") or context.source_target.transport_model,
                server_id=provider.get("omlx_server_id"),
            )
        except OmlxLoadedStateError:
            return warm_capture_refused(request.thread_id, "native", "source_not_loaded")
    warm_source = await capture_warm_checkpoint_source(
        request.thread_id,
        context.boundary,
        target=context.source_target,
        binary=context.source_binary,
        reasoning_effort=context.reasoning_effort,
    )
    if warm_source is None:
        return None
    return build_warm_admission(
        checkpoint_id=checkpoint_id,
        thread_id=request.thread_id,
        turn=None,
        trigger="model_switch",
        target=context.source_target,
        boundary=context.boundary,
        previous_generation_id=(context.previous.id if context.previous else None),
        working_dir=context.working_dir,
        reasoning_effort=context.reasoning_effort,
        binary=context.source_binary,
        warm_source=warm_source,
    )


def _target_cold_admission(
    request: SwitchRequest,
    checkpoint_id: str,
) -> CheckpointAdmission:
    from core.checkpoint_cold_admission import build_checkpoint_admission
    from infra.database import get_db
    from llm.resolution import ExecutionTarget

    context = read_switch_checkpoint_context(request)
    destination = cast(ExecutionTarget, context.destination_target)
    db = get_db()
    try:
        admission = build_checkpoint_admission(
            checkpoint_id=checkpoint_id,
            thread_id=request.thread_id,
            trigger="model_switch",
            source_target=context.source_target,
            source_model=request.source_model,
            writer_target=context.destination_target,
            boundary=context.boundary,
            previous=context.previous,
            projection=context.projection,
            working_dir=context.working_dir,
            reasoning_effort=context.reasoning_effort,
            binary=context.destination_binary,
            mcp_config=context.destination_mcp_config,
            db=db,
        )
        from connections.vendor_accounts import VENDORS

        if destination.harness not in VENDORS:
            return admission
        from connections.account_policy import turn_account

        account_id, _reason = turn_account(
            destination.harness,
            thread_id=request.thread_id,
            model=destination.catalog_id,
            db=db,
        )
        return replace(
            admission,
            provider_request=replace(
                admission.provider_request,
                vendor_account_id=account_id,
                vendor_account_bound=True,
            ),
        )
    finally:
        db.close()


async def prepare_target_checkpoint(
    request: SwitchRequest,
    checkpoint_id: str,
) -> CheckpointAdmission:
    """Prepare the one selected-destination cold request and fixed local fit."""
    from infra.db_async import run_in_db

    return await run_in_db(_target_cold_admission, request, checkpoint_id)


def relevant_checkpoint(request: SwitchRequest) -> CheckpointSummary | None:
    """Read this switch's newest derived attempt without storing another owner."""
    from infra.checkpoint_queries import model_switch_for_thread
    from infra.database import get_db

    db = get_db()
    try:
        source, destination = _switch_targets(db, request)
        return model_switch_for_thread(
            request.thread_id,
            source_model=None if source is None else source.catalog_id,
            cold_source_model=request.source_model,
            destination_model=destination.catalog_id,
            requested_at=request.requested_at,
            db=db,
        )
    finally:
        db.close()


def obsolete_source_checkpoint(request: SwitchRequest, record: CheckpointRecord) -> bool:
    """Qualify only an unusable source's exact warm fork when its slot is needed."""
    if request.checkpoint_choice != "target" or record.mode != "warm":
        return False
    from infra.database import get_db
    from llm.resolution import ModelUnavailableError, resolve_execution_target

    db = get_db()
    try:
        row = db.execute("SELECT * FROM threads WHERE id=?", (request.thread_id,)).fetchone()
        if row is None or row["switch_operation_id"] != request.operation_id:
            return False
        try:
            source = resolve_execution_target(request.source_model, db=db)
        except (ModelUnavailableError, ValueError):
            return False
        return (
            record.thread_id == request.thread_id
            and record.history_epoch == row["history_epoch"]
            and record.source_harness == record.writer_harness == source.harness
            and record.source_model == record.writer_model == source.catalog_id
        )
    finally:
        db.close()


def new_checkpoint_id() -> str:
    """Name one immediate admission attempt."""
    return str(uuid4())
