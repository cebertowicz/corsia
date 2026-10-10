"""Shared checkpoint preparation for manual and agent-initiated external requests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from core.checkpoint_fit import ColdFoldInputTooLarge
from core.checkpoint_types import CheckpointRequest, CheckpointTrigger, ProviderCheckpointRequest
from history.types import HistoryBoundary
from infra.checkpoint_admission import IDLE_MIN_DELTA_TOKENS, admits_idle_checkpoint
from llm.resolution import ExecutionTarget


@dataclass(frozen=True, slots=True)
class CheckpointPreparationContext:
    """Durable facts shared by the manual endpoint and agent tool."""

    thread_id: str
    target: ExecutionTarget
    boundary: HistoryBoundary
    previous: Any | None
    projection: Any
    previous_generation_id: str | None
    working_dir: str
    reasoning_effort: str | None
    binary: str | None
    foldable_tokens: int


class CheckpointNotReady(RuntimeError):
    """A requested checkpoint cannot be admitted from the current durable head."""

    def __init__(self, message: str, *, reason_key: str = "not_ready", **details) -> None:
        super().__init__(message)
        self.reason_key = reason_key
        self.details = details


_REASON_MESSAGES = {
    "no_sealed_boundary": "checkpoint requires a sealed message boundary",
    "no_new_history": "checkpoint already covers the sealed history",
}


def _reason_message(eligibility) -> str:
    if eligibility.reason_key == "model_unavailable":
        target = eligibility.target
        return (
            (target.availability_reason if target else None)
            or "selected checkpoint model is unavailable"
        )
    return _REASON_MESSAGES.get(
        eligibility.reason_key, "checkpoint is not currently available"
    )


def read_checkpoint_preparation(
    thread_id: str,
    user_id: str,
    *,
    trigger: CheckpointTrigger,
) -> CheckpointPreparationContext | None:
    """Read one user-owned external checkpoint boundary and cost measurement."""
    from core.checkpoint_eligibility import checkpoint_eligibility
    from core.checkpoint_render import checkpoint_binary
    from infra.database import get_db
    from subagents.codex_runtime_support import resolve_working_dir

    db = get_db()
    try:
        db.execute("BEGIN")
        eligibility = checkpoint_eligibility(thread_id, user_id, db=db)
        if eligibility is None:
            return None
        if not eligibility.eligible:
            raise CheckpointNotReady(
                _reason_message(eligibility), reason_key=eligibility.reason_key,
            )
        thread_row = eligibility.thread_row
        target = eligibility.target
        previous = eligibility.previous
        foldable_tokens = 0
        if trigger == "agent":
            from core.checkpoint_fit import foldable_tail_tokens

            foldable_tokens = foldable_tail_tokens(
                eligibility.projection,
                HistoryBoundary.of_checkpoint(previous) if previous else None,
                model=target.expected_reported_model or target.transport_model,
            )
            if not admits_idle_checkpoint(foldable_tokens):
                raise CheckpointNotReady(
                    "not enough new material to be worth a paid fold",
                    reason_key="below_minimum",
                    foldable_tokens=foldable_tokens,
                    threshold_tokens=IDLE_MIN_DELTA_TOKENS,
                )
        from llm.reasoning_config import effective_reasoning_effort

        return CheckpointPreparationContext(
            thread_id=thread_id,
            target=target,
            boundary=eligibility.boundary,
            previous=previous,
            projection=eligibility.projection,
            previous_generation_id=previous.id if previous is not None else None,
            working_dir=resolve_working_dir(thread_row["project_id"], db),
            reasoning_effort=effective_reasoning_effort(
                thread_row["reasoning_effort"], target.catalog_id,
            ),
            binary=checkpoint_binary(target, db),
            foldable_tokens=foldable_tokens,
        )
    finally:
        db.close()


def _build_manual_cold_admission(
    checkpoint_id: str,
    context: CheckpointPreparationContext,
):
    """Build manual cold admission through the shared checkpoint renderer."""
    from core.checkpoint_render import build_cold_checkpoint_admission
    from history.projection import slice_portable_projection
    from infra.database import get_db

    db = get_db()
    try:
        return build_cold_checkpoint_admission(
            checkpoint_id=checkpoint_id,
            thread_id=context.thread_id,
            trigger="manual",
            source_target=context.target,
            writer_target=context.target,
            boundary=context.boundary,
            previous=context.previous,
            projection=slice_portable_projection(
                context.projection, through=context.boundary,
            ),
            working_dir=context.working_dir,
            reasoning_effort=context.reasoning_effort,
            binary=context.binary,
            db=db,
        )
    finally:
        db.close()


def _manual_cold_refusal(model: str) -> CheckpointNotReady:
    """Name the cause of a cold fold that cannot fit; blame the cap only when it alone fails."""
    from infra.database import get_db
    from infra.input_admission import (
        CHECKPOINT_CONFIGURATION_IMPOSSIBLE, checkpoint_cap_impossible, checkpoint_cap_refusal,
    )
    from llm.context_window import get_context_window_or_none

    db = get_db()
    try:
        window = get_context_window_or_none(model, db=db)
    finally:
        db.close()
    if window is None:
        return CheckpointNotReady(
            f"The context window for {model} is not known yet, so a checkpoint "
            "cannot be sized. Try again shortly or select another model.",
            reason_key="checkpoint_window_unknown",
        )
    if checkpoint_cap_impossible(window, model):
        message, legal_action = checkpoint_cap_refusal(model)
        return CheckpointNotReady(
            message, reason_key=CHECKPOINT_CONFIGURATION_IMPOSSIBLE,
            legal_action=legal_action,
        )
    legal_action = "Select a model with a larger context window."
    return CheckpointNotReady(
        f"The newest turn in this thread is too large for a checkpoint on {model}. "
        f"{legal_action}",
        reason_key="checkpoint_input_too_large", legal_action=legal_action,
    )


async def prepare_external_checkpoint(
    thread_id: str,
    user_id: str,
    *,
    trigger: CheckpointTrigger,
) -> tuple[CheckpointRequest, ProviderCheckpointRequest] | None:
    """Prepare a requested external checkpoint without starting its operation."""
    from core.checkpoint_render import capture_warm_checkpoint_source
    from infra.db_async import run_in_db
    from infra.time import utc_now_iso
    from prompts.checkpoint import build_checkpoint_system_prompt

    context = await run_in_db(
        read_checkpoint_preparation, thread_id, user_id, trigger=trigger,
    )
    if context is None:
        return None
    source = await capture_warm_checkpoint_source(
        context.thread_id,
        context.boundary,
        target=context.target,
        binary=context.binary,
        reasoning_effort=context.reasoning_effort,
    )
    checkpoint_id = str(uuid4())
    if source is None:
        if trigger == "manual":
            try:
                admission = await run_in_db(
                    _build_manual_cold_admission, checkpoint_id, context,
                )
            except ColdFoldInputTooLarge as error:
                raise await run_in_db(
                    _manual_cold_refusal, context.target.catalog_id,
                ) from error
            return admission.request, admission.provider_request
        raise CheckpointNotReady(
            "current provider continuity could not admit a warm checkpoint",
            reason_key="warm_capture_refused",
        )
    request = CheckpointRequest(
        id=checkpoint_id,
        thread_id=thread_id,
        history_epoch=context.boundary.history_epoch,
        previous_generation_id=context.previous_generation_id,
        through_seq=context.boundary.seq,
        through_message_id=context.boundary.message_id,
        trigger=trigger,
        mode="warm",
        source_harness=context.target.harness,
        source_model=context.target.catalog_id,
        writer_harness=context.target.harness,
        writer_model=context.target.catalog_id,
        originating_turn_id=None,
        first_included_seq=None,
        started_at=utc_now_iso(),
    )
    provider = ProviderCheckpointRequest(
        checkpoint_id=checkpoint_id,
        thread_id=thread_id,
        target=context.target,
        instruction=build_checkpoint_system_prompt(),
        reasoning_effort=context.reasoning_effort,
        working_dir=context.working_dir,
        binary=context.binary,
        warm_source=source,
    )
    return request, provider
