"""One provider-call checkpoint decision beneath an existing CORSIA Turn."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from uuid import uuid4

from loguru import logger

from core.fork_fit import (
    BoundaryDecision, ColdFoldInputTooLarge, ProviderPayload, evaluate_boundary,
)
from core.checkpoint_wait import CheckpointWaitScope, await_checkpoint_or_stop
from core.checkpoint_warm_request import CheckpointAdmission
from core.checkpoint_types import (
    CheckpointContinuationError,
    CheckpointRecord,
    CheckpointUnavailable,
    OptionalCheckpointSkipped,
)
from core.turn_core import TurnIdentity
from history.types import HistoryBoundary


@dataclass(frozen=True, slots=True)
class InstalledContinuation:
    """One rendered installed checkpoint plus tail for the selected provider."""

    input_tokens: int
    payload: object


CHECKPOINT_TAIL_TOO_LARGE = "checkpoint plus tail does not fit the selected provider"

PrepareCheckpoint = Callable[[str, bool], Awaitable[CheckpointAdmission | None]]
RenderInstalled = Callable[[CheckpointRecord], Awaitable[InstalledContinuation]]


@dataclass(frozen=True, slots=True)
class ProviderBoundary:
    """All proved facts and two provider render seams for one next call."""

    thread_id: str
    turn: TurnIdentity
    context_window: int
    canonical_input_tokens: int | None
    provider_input_tokens: int | None
    minimum_input_tokens: int
    adopted_checkpoint_id: str | None
    active_checkpoint: CheckpointRecord | None
    installed_checkpoint: CheckpointRecord | None
    prepare_checkpoint: PrepareCheckpoint | None
    render_installed: RenderInstalled
    wait_scope: CheckpointWaitScope
    eager_checkpoint_id: str | None = None
    recovery_input_tokens: int | None = None
    fixed_input_tokens: int = 0
    #: The configured window when ``context_window`` is a live memory fit, which
    #: ends with this Turn; settlement compares configured windows only.
    configured_window: int | None = None

    def unavailable(
        self, detail: str, *, limited_target: str | None = None,
        limited_connection: str | None = None,
    ) -> CheckpointUnavailable:
        """Carry the observed facts to the existing input settlement owner."""
        return CheckpointUnavailable(
            detail, context_window=self.configured_window or self.context_window,
            installed_checkpoint_id=(
                self.installed_checkpoint.id if self.installed_checkpoint else None
            ),
            limited_target=limited_target, limited_connection=limited_connection,
        )


@dataclass(frozen=True, slots=True)
class ProviderContinuation:
    """The next action without changing the owning CORSIA Turn."""

    decision: BoundaryDecision
    turn: TurnIdentity
    payload: object | None = None
    checkpoint_id: str | None = None


def _validate_admission(
    boundary: ProviderBoundary,
    checkpoint_id: str,
    admission: CheckpointAdmission,
) -> None:
    request = admission.request
    if request.id != checkpoint_id or request.thread_id != boundary.thread_id:
        raise ValueError("checkpoint admission identity differs from boundary")
    if request.originating_turn_id != boundary.turn.turn_id:
        raise ValueError("checkpoint admission differs from the exact Turn")


async def _start_checkpoint(
    boundary: ProviderBoundary,
    *,
    required: bool,
) -> CheckpointRecord | None:
    if boundary.prepare_checkpoint is None:
        if required:
            raise boundary.unavailable(
                "the next provider call does not fit and no checkpoint is admissible"
            )
        return None
    from core.checkpoint_runtime import (
        ForkNotAdmitted,
        CheckpointTargetLimited,
        request_checkpoint,
    )
    from core.fork_types import NothingNewerToFold

    checkpoint_id = str(uuid4())
    try:
        admission = await boundary.prepare_checkpoint(checkpoint_id, required)
        if admission is None:
            raise ForkNotAdmitted("checkpoint admission is unavailable")
        _validate_admission(boundary, checkpoint_id, admission)
        return await request_checkpoint(admission.request, admission.provider_request)
    except OptionalCheckpointSkipped as skipped:
        if required:
            raise ValueError("a required checkpoint cannot be skipped") from skipped
        logger.info("Optional checkpoint skipped for {}: {}", boundary.thread_id, skipped)
        return None
    except CheckpointTargetLimited as error:
        if required:
            raise boundary.unavailable(
                str(error), limited_target=error.model,
                limited_connection=error.connection,
            ) from error
        logger.info(
            "Optional checkpoint skipped for {}: target_limited {}",
            boundary.thread_id, error,
        )
        return None
    except (ColdFoldInputTooLarge, ForkNotAdmitted) as error:
        if required and isinstance(error, NothingNewerToFold):
            raise  # the provider's boundary owner decides what a fold that cannot start means
        if required:
            raise boundary.unavailable(str(error)) from error
        logger.warning("Optional checkpoint unavailable for {}: {}", boundary.thread_id, error)
        return None


async def _installed_candidate(
    boundary: ProviderBoundary,
    record: CheckpointRecord | None,
) -> InstalledContinuation | None:
    if record is None:
        return None
    candidate = await boundary.render_installed(record)
    if candidate.input_tokens < 0:
        raise ValueError("installed continuation tokens must be non-negative")
    return candidate


def _payload(
    boundary: ProviderBoundary,
    active: CheckpointRecord | None,
    installed: InstalledContinuation | None,
) -> ProviderPayload:
    canonical = boundary.canonical_input_tokens
    if canonical is None:
        canonical = boundary.provider_input_tokens
    if canonical is None:
        raise CheckpointContinuationError(
            "the next provider input cannot be measured or rendered"
        )
    return ProviderPayload(
        context_window=boundary.context_window,
        canonical_input_tokens=canonical,
        input_tokens=boundary.provider_input_tokens,
        minimum_tokens=boundary.minimum_input_tokens,
        checkpoint_status=active.status if active is not None else None,
        installed_input_tokens=(installed.input_tokens if installed else None),
        checkpoint_admissible=boundary.prepare_checkpoint is not None,
        recovery_input_tokens=boundary.recovery_input_tokens,
        fixed_tokens=boundary.fixed_input_tokens,
    )


async def _await_installed(
    boundary: ProviderBoundary,
    record: CheckpointRecord,
    *,
    allow_newer_fold: bool = True,
) -> ProviderContinuation:
    from core.checkpoint_runtime import await_checkpoint

    # An attempt this Turn started and is blocked on is part of its work, so
    # Stop ends it; an attempt from anywhere else stays independent (#1921).
    owned = record.originating_turn_id == boundary.turn.turn_id
    completed = await await_checkpoint_or_stop(
        boundary.wait_scope,
        record.id,
        await_checkpoint,
        on_stop=cancel_waiting_checkpoint if owned else None,
    )
    if completed.status != "installed":
        raise boundary.unavailable(
            completed.error or f"checkpoint ended {completed.status}"
        )
    boundary = replace(boundary, installed_checkpoint=completed)
    installed = await boundary.render_installed(completed)
    if installed.input_tokens > boundary.context_window:
        if not allow_newer_fold:
            raise boundary.unavailable(CHECKPOINT_TAIL_TOO_LARGE)
        started = await _refold_over_installed(boundary)
        return await _await_installed(
            boundary,
            started,
            allow_newer_fold=False,
        )
    return ProviderContinuation(
        BoundaryDecision.ADOPT_AND_PROCEED,
        boundary.turn,
        installed.payload,
        completed.id,
    )


async def _required_checkpoint_continuation(
    boundary: ProviderBoundary,
    active: CheckpointRecord | None,
    installed_record: CheckpointRecord | None,
) -> ProviderContinuation:
    installed = await _installed_candidate(boundary, installed_record)
    if installed is not None and installed.input_tokens <= boundary.context_window:
        return ProviderContinuation(
            BoundaryDecision.ADOPT_AND_PROCEED,
            boundary.turn,
            installed.payload,
            installed_record.id if installed_record is not None else None,
        )
    if active is not None:
        return await _await_installed(boundary, active)
    if installed is not None:
        return await _await_installed(boundary, await _refold_over_installed(boundary))
    started = await _start_checkpoint(boundary, required=True)
    assert started is not None
    return await _await_installed(boundary, started)


async def _refold_over_installed(boundary: ProviderBoundary) -> CheckpointRecord:
    """Fold past an installed checkpoint measured over the window; nothing newer means its tail cannot fit."""
    from core.fork_types import NothingNewerToFold

    try:
        started = await _start_checkpoint(boundary, required=True)
    except NothingNewerToFold as error:
        raise boundary.unavailable(CHECKPOINT_TAIL_TOO_LARGE) from error
    assert started is not None
    return started


async def maintain_before_provider_call(
    boundary: ProviderBoundary,
    *,
    must_adopt: bool = False,
) -> ProviderContinuation:
    """Proceed, start background work, or await one checkpoint at this boundary."""
    active = boundary.active_checkpoint
    installed_record = boundary.installed_checkpoint
    current_tokens = boundary.provider_input_tokens
    if current_tokens is None:
        current_tokens = boundary.canonical_input_tokens
    if current_tokens is None:
        raise CheckpointContinuationError(
            "the next provider input cannot be measured or rendered"
        )
    if must_adopt or (
        boundary.eager_checkpoint_id is not None
        and boundary.eager_checkpoint_id != boundary.adopted_checkpoint_id
    ):
        return await _required_checkpoint_continuation(
            boundary, active, installed_record
        )
    installed = None
    if current_tokens > boundary.context_window:
        installed = await _installed_candidate(boundary, installed_record)
    decision = evaluate_boundary(_payload(boundary, active, installed))
    if decision is BoundaryDecision.PROCEED:
        return ProviderContinuation(decision, boundary.turn)
    if decision is BoundaryDecision.ADOPT_AND_PROCEED:
        assert installed_record is not None and installed is not None
        return ProviderContinuation(
            decision, boundary.turn, installed.payload, installed_record.id
        )
    if decision is BoundaryDecision.PROCEED_AND_FOLD:
        started = await _start_checkpoint(boundary, required=False)
        return ProviderContinuation(
            decision if started else BoundaryDecision.PROCEED,
            boundary.turn,
            checkpoint_id=started.id if started else None,
        )
    if decision is BoundaryDecision.FAIL_VISIBLE:
        raise boundary.unavailable(
            "the minimum provider continuation exceeds the context window"
        )
    if active is not None:
        return await _await_installed(boundary, active)
    started = await _start_checkpoint(boundary, required=True)
    assert started is not None
    return await _await_installed(boundary, started)


async def cancel_waiting_checkpoint(
    scope: CheckpointWaitScope,
) -> CheckpointRecord | None:
    """Cancel only the hard-wall checkpoint synchronously owned by this scope."""
    checkpoint_id = scope.checkpoint_id
    if checkpoint_id is None:
        return None
    from core.checkpoint_runtime import cancel_checkpoint

    return await cancel_checkpoint(checkpoint_id)


def installed_replay(
    record: CheckpointRecord,
    *,
    db=None,
    exclude_message_id: str | None = None,
):
    """Render one installed artifact followed by its exact durable tail."""
    selection, _exact_seal = installed_replay_with_exact_seal(
        record, db=db, exclude_message_id=exclude_message_id,
    )
    return selection


def installed_replay_with_exact_seal(
    record: CheckpointRecord,
    *,
    db=None,
    exclude_message_id: str | None = None,
    timing=None,
    timing_prefix: str = "checkpoint_tail",
):
    """Render the installed replay plus the seq-0 exact replay's sealed boundary.

    The seal comes from the same snapshot as the tail, so a caller can defer the
    exact replay and still pin its identity.
    """
    from core.checkpoint_render import render_installed_checkpoint
    from core.context_replay import ReplaySelection
    from history.projection import render_checkpoint_tail
    from infra.checkpoint_receipts import frozen_presence_line
    from infra.context_preview_timing import timed
    from infra.database import get_db

    boundary = HistoryBoundary.of_checkpoint(record)
    connection = db or get_db()
    try:
        tail, exact_seal = timed(
            timing,
            f"{timing_prefix}.tail_render",
            render_checkpoint_tail,
            record.thread_id,
            db=connection,
            start_exclusive=boundary,
            exclude_message_id=exclude_message_id,
        )
        # Frozen at install; rendered only where the tail opens mid-Turn.
        presence_line = timed(
            timing,
            f"{timing_prefix}.presence_line",
            frozen_presence_line,
            connection,
            record.id,
        )
        tail = replace(tail, resume_presence_line=presence_line)
    finally:
        if db is None:
            connection.close()
    return ReplaySelection(
        mode="checkpoint_tail",
        history_epoch=record.history_epoch,
        sealed_boundary=tail.sealed_boundary,
        tail=tail,
        checkpoint_generation_id=record.id,
        checkpoint_boundary=boundary,
        checkpoint_json=timed(
            timing,
            f"{timing_prefix}.checkpoint_render",
            render_installed_checkpoint,
            record,
        ),
    ), exact_seal
