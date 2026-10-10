"""Warm checkpoint request provenance frozen before provider preparation."""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from core.checkpoint_types import (
    CheckpointRequest,
    CheckpointTrigger,
    ProviderCheckpointRequest,
)
from core.turn_core import TurnIdentity
from history.types import HistoryBoundary
from llm.resolution import ExecutionTarget


@dataclass(frozen=True, slots=True)
class CheckpointAdmission:
    """One complete checkpoint operation prepared at a provider boundary."""

    request: CheckpointRequest
    provider_request: ProviderCheckpointRequest


def build_warm_request(
    *,
    checkpoint_id: str,
    thread_id: str,
    turn: TurnIdentity | None,
    trigger: CheckpointTrigger = "pressure",
    target: ExecutionTarget,
    boundary: HistoryBoundary,
    previous_generation_id: str | None,
) -> CheckpointRequest:
    """Freeze checkpoint provenance before any fallible provider preparation."""
    from infra.time import utc_now_iso

    return CheckpointRequest(
        id=checkpoint_id,
        thread_id=thread_id,
        history_epoch=boundary.history_epoch,
        previous_generation_id=previous_generation_id,
        through_seq=boundary.seq,
        through_message_id=boundary.message_id,
        trigger=trigger,
        mode="warm",
        source_harness=target.harness,
        source_model=target.catalog_id,
        writer_harness=target.harness,
        writer_model=target.catalog_id,
        originating_turn_id=turn.turn_id if turn is not None else None,
        first_included_seq=None,
        started_at=utc_now_iso(),
    )


def warm_capture_refused(thread_id: str, harness: str, reason: str) -> None:
    """Log the one reason a warm source was not captured, then return None."""
    logger.info(
        "checkpoint warm capture refused for {} ({}): {}",
        thread_id, harness, reason,
    )
    return None


def complete_warm_admission(
    request: CheckpointRequest,
    *,
    target: ExecutionTarget,
    working_dir: str,
    reasoning_effort: str | None,
    binary: str | None,
    warm_source: object,
) -> CheckpointAdmission:
    """Pair frozen provenance with one already-captured warm provider source."""
    from prompts.checkpoint import build_checkpoint_system_prompt

    provider = ProviderCheckpointRequest(
        checkpoint_id=request.id,
        thread_id=request.thread_id,
        target=target,
        instruction=build_checkpoint_system_prompt(),
        reasoning_effort=reasoning_effort,
        working_dir=working_dir,
        binary=binary,
        warm_source=warm_source,
    )
    return CheckpointAdmission(request, provider)


def build_warm_admission(
    *,
    checkpoint_id: str,
    thread_id: str,
    turn: TurnIdentity | None,
    trigger: CheckpointTrigger = "pressure",
    target: ExecutionTarget,
    boundary: HistoryBoundary,
    previous_generation_id: str | None,
    working_dir: str,
    reasoning_effort: str | None,
    binary: str | None,
    warm_source: object,
) -> CheckpointAdmission:
    """Build one warm checkpoint operation from already-frozen provider input."""
    request = build_warm_request(
        checkpoint_id=checkpoint_id,
        thread_id=thread_id,
        turn=turn,
        trigger=trigger,
        target=target,
        boundary=boundary,
        previous_generation_id=previous_generation_id,
    )
    return complete_warm_admission(
        request,
        target=target,
        working_dir=working_dir,
        reasoning_effort=reasoning_effort,
        binary=binary,
        warm_source=warm_source,
    )
