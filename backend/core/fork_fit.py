"""One cross-harness call boundary and one bounded cold-input ladder."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum

from core.fork_pressure import (
    CHECKPOINT_INSTRUCTION_BUDGET_TOKENS, WARM_FORK_RESERVE_TOKENS,
    at_checkpoint_launch_point, soft_pressure_remaining,
)
from core.token_estimation import _estimate_messages_tokens
from core.checkpoint_fold_blocks import (
    COLD_FOLD_OLD_TOOL_BUDGET_TOKENS,
    COLD_FOLD_TOOL_REFERENCE_BUDGET_TOKENS,
    mid_turn_resume_blocks,
    omission_block,
    resume_anchor_block,
    source_blocks,
    unit_messages,
)
from core.tool_result_projection import HARNESS_TOOL_RESULT_BUDGET_TOKENS
from history.adapters import checkpoint_source_block, merge_checkpoint_source_blocks
from history.folds import checkpoint_fold_units
from history.types import (
    CheckpointFoldUnit,
    HistoryBoundary,
    PortableHistoryProjection,
)


class BoundaryDecision(str, Enum):
    PROCEED = "proceed"
    PROCEED_AND_FOLD = "proceed_and_fold"
    WAIT = "wait"
    ADOPT_AND_PROCEED = "adopt_and_proceed"
    FAIL_VISIBLE = "fail_visible"


@dataclass(frozen=True, slots=True)
class ProviderPayload:
    """The proved facts needed at one provider-call boundary.

    ``input_tokens`` is the latest correlated provider measurement.
    ``canonical_input_tokens`` is the renderer-owned conservative count used
    only when that measurement is absent.
    """

    context_window: int
    canonical_input_tokens: int
    input_tokens: int | None
    minimum_tokens: int
    checkpoint_status: str | None
    installed_input_tokens: int | None
    checkpoint_admissible: bool
    recovery_input_tokens: int | None = None
    # The system prompt and guidance every continuation also carries; never history.
    fixed_tokens: int = 0
    # The newest installed continuation's size when first adopted; None: unknown.
    adopted_input_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ColdFoldInput:
    """One final cold request body and how many leading fold units it omits.

    The cut is in delivery order, so ``first_included_seq`` is the lowest
    included seq, not a seq below which everything was omitted.
    """

    messages: tuple[dict[str, str], ...]
    input_tokens: int
    first_included_seq: int | None
    omitted_units: int

    def as_prompt(self, instruction: str) -> str:
        """Flatten this fitted source for either external cold writer."""
        source = "\n\n".join(message["content"] for message in self.messages)
        return f"{instruction}\n\n{source}" if self.messages else instruction


class ColdFoldInputTooLarge(RuntimeError):
    """Even the protected minimum cannot fit one fork call."""


def foldable_tail_tokens(
    projection: PortableHistoryProjection,
    boundary: HistoryBoundary | None,
    *,
    project_arguments: bool = False,
    model: str | None = None,
) -> int:
    """Measure the foldable material a projection carries after one boundary.

    This is the same estimate the fold itself would pay for, so a caller
    deciding whether new work is worth folding and the fold that follows agree
    on what "enough" means. ``boundary`` of ``None`` measures the whole
    projection.
    """
    lower = boundary.seq if boundary is not None else 0
    blocks = [
        checkpoint_source_block(
            message,
            thread_id=projection.thread_id,
            tool_result_budget_tokens=HARNESS_TOOL_RESULT_BUDGET_TOKENS,
            tool_argument_budget_tokens=(
                HARNESS_TOOL_RESULT_BUDGET_TOKENS if project_arguments else None
            ),
        )
        for message in projection.messages
        if message.seq > lower
    ]
    return _estimate_messages_tokens(blocks, model=model)


def _optional_tokens(value: int | None, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def evaluate_boundary(payload: ProviderPayload) -> BoundaryDecision:
    """Choose one action from exact input fit and current checkpoint state."""
    correlated = _optional_tokens(payload.input_tokens, "correlated input tokens")
    canonical = payload.canonical_input_tokens
    minimum = payload.minimum_tokens
    installed = _optional_tokens(
        payload.installed_input_tokens, "installed input tokens"
    )
    current = correlated if correlated is not None else canonical
    recovery = _optional_tokens(payload.recovery_input_tokens, "recovery input tokens")
    if current <= payload.context_window:
        active = payload.checkpoint_status in {"running", "captured"}
        under_pressure = at_checkpoint_launch_point(max(current, recovery or 0), payload.context_window)
        # R08: an optional fold cannot relieve pressure when even the smallest
        # continuation it could produce would sit at the launch point itself.
        futile = at_checkpoint_launch_point(minimum + payload.fixed_tokens, payload.context_window)
        # The newest fold already landed at the launch point under real tail
        # growth, so another optional one would only chase it back to back.
        futile = futile or (
            payload.adopted_input_tokens is not None
            and at_checkpoint_launch_point(payload.adopted_input_tokens, payload.context_window)
        )
        if under_pressure and payload.checkpoint_admissible and not active and not futile:
            return BoundaryDecision.PROCEED_AND_FOLD
        return BoundaryDecision.PROCEED
    if installed is not None and installed <= payload.context_window:
        return BoundaryDecision.ADOPT_AND_PROCEED
    if minimum > payload.context_window:
        return BoundaryDecision.FAIL_VISIBLE
    if payload.checkpoint_status in {"running", "captured"}:
        return BoundaryDecision.WAIT
    if payload.checkpoint_admissible:
        return BoundaryDecision.WAIT
    return BoundaryDecision.FAIL_VISIBLE


def _candidate(
    source: PortableHistoryProjection,
    units: Sequence[CheckpointFoldUnit],
    *,
    skipped: int,
    project_tools: bool,
    newest_tool_budget_tokens: int,
    protected_blocks: Sequence[dict[str, str]],
    count_tokens: Callable[[Sequence[dict[str, str]]], int],
    old_tool_budget_tokens: int = COLD_FOLD_OLD_TOOL_BUDGET_TOKENS,
    stub_old_tool_results: bool = False,
    reference_tool_arguments: bool = False,
) -> ColdFoldInput | None:
    included = units[skipped:]
    if not included:
        return None
    blocks = [dict(block) for block in protected_blocks]
    if skipped:
        blocks.append(omission_block())
        blocks.extend(mid_turn_resume_blocks(
            units[:skipped], included, thread_id=source.thread_id,
            presence_line=source.resume_presence_line,
        ))
    elif source.turns and not source.turns[0].includes_turn_start:
        blocks.append(resume_anchor_block(source.resume_presence_line))
    blocks.extend(
        source_blocks(
            source,
            included,
            project_tools=project_tools,
            newest_ordinal=units[-1].ordinal,
            newest_tool_budget_tokens=newest_tool_budget_tokens,
            old_tool_budget_tokens=old_tool_budget_tokens,
            stub_old_tool_results=stub_old_tool_results,
            reference_tool_arguments=reference_tool_arguments,
        )
    )
    messages = tuple(merge_checkpoint_source_blocks(blocks))
    measured = count_tokens(messages)
    if isinstance(measured, bool) or not isinstance(measured, int) or measured < 0:
        raise ValueError("cold-fold token count must be a non-negative integer")
    first_seq = min(message.seq for message in unit_messages(included))
    return ColdFoldInput(messages, measured, first_seq, skipped)


def _oldest_fitting_candidate(
    source: PortableHistoryProjection,
    units: Sequence[CheckpointFoldUnit],
    skips: Sequence[int],
    *,
    max_input_tokens: int,
    newest_tool_budget_tokens: int,
    protected_blocks: Sequence[dict[str, str]],
    count_tokens: Callable[[Sequence[dict[str, str]]], int],
    old_tool_budget_tokens: int = COLD_FOLD_OLD_TOOL_BUDGET_TOKENS,
    stub_old_tool_results: bool = False,
    reference_tool_arguments: bool = False,
) -> tuple[ColdFoldInput | None, int | None]:
    """Binary-search one suffix range whose rendered sizes only decrease."""
    first, last = 0, len(skips) - 1
    fitted = None
    fitted_skip = None
    while first <= last:
        middle = (first + last) // 2
        skipped = skips[middle]
        candidate = _candidate(
            source,
            units,
            skipped=skipped,
            project_tools=True,
            newest_tool_budget_tokens=newest_tool_budget_tokens,
            protected_blocks=protected_blocks,
            count_tokens=count_tokens,
            old_tool_budget_tokens=old_tool_budget_tokens,
            stub_old_tool_results=stub_old_tool_results,
            reference_tool_arguments=reference_tool_arguments,
        )
        if candidate is not None and candidate.input_tokens <= max_input_tokens:
            fitted, fitted_skip = candidate, skipped
            last = middle - 1
        else:
            first = middle + 1
    return fitted, fitted_skip


def _final_turn_fit(
    source: PortableHistoryProjection,
    units: Sequence[CheckpointFoldUnit],
    *,
    final_start: int,
    max_input_tokens: int,
    protected_blocks: Sequence[dict[str, str]],
    count_tokens: Callable[[Sequence[dict[str, str]]], int],
) -> ColdFoldInput | None:
    """Fit the final turn, then bound or stub tool results across its suffix."""
    # The newest unit normally keeps the full harness budget, so one step holding many
    # tool results can exceed the window alone. These rungs are the floor below that.
    # `whole` stays a single searched point and is never folded into `interior`:
    # dropping the final turn's first unit re-adds the omission envelope and its
    # untruncated initiating input, so candidate sizes are not monotonic across that
    # boundary and the binary search could step over one that fits.
    interior = range(final_start + 1, len(units))
    whole = (final_start,)
    for skips, newest_tool_budget_tokens in (
        (interior, HARNESS_TOOL_RESULT_BUDGET_TOKENS),
        (whole, COLD_FOLD_OLD_TOOL_BUDGET_TOKENS),
        (interior, COLD_FOLD_OLD_TOOL_BUDGET_TOKENS),
        (whole, COLD_FOLD_OLD_TOOL_BUDGET_TOKENS // 2),
        (interior, COLD_FOLD_OLD_TOOL_BUDGET_TOKENS // 2),
    ):
        fitted, _ = _oldest_fitting_candidate(
            source,
            units,
            skips,
            max_input_tokens=max_input_tokens,
            newest_tool_budget_tokens=newest_tool_budget_tokens,
            protected_blocks=protected_blocks,
            count_tokens=count_tokens,
        )
        if fitted is not None:
            return fitted
    for stub_old_tool_results in (False, True):
        reference_floor = _candidate(
            source,
            units,
            skipped=0,
            project_tools=True,
            newest_tool_budget_tokens=COLD_FOLD_TOOL_REFERENCE_BUDGET_TOKENS,
            old_tool_budget_tokens=COLD_FOLD_TOOL_REFERENCE_BUDGET_TOKENS,
            stub_old_tool_results=stub_old_tool_results,
            reference_tool_arguments=True,
            protected_blocks=protected_blocks,
            count_tokens=count_tokens,
        )
        if reference_floor is not None and reference_floor.input_tokens <= max_input_tokens:
            return reference_floor
        for skips in (whole, interior):
            fitted, _ = _oldest_fitting_candidate(
                source,
                units,
                skips,
                max_input_tokens=max_input_tokens,
                newest_tool_budget_tokens=COLD_FOLD_TOOL_REFERENCE_BUDGET_TOKENS,
                old_tool_budget_tokens=COLD_FOLD_TOOL_REFERENCE_BUDGET_TOKENS,
                stub_old_tool_results=stub_old_tool_results,
                reference_tool_arguments=True,
                protected_blocks=protected_blocks,
                count_tokens=count_tokens,
            )
            if fitted is not None:
                return fitted
    return None


def render_cold_fold_input(
    source: PortableHistoryProjection,
    *,
    max_input_tokens: int,
    protected_blocks: Sequence[dict[str, str]] = (),
    count_tokens: Callable[[Sequence[dict[str, str]]], int] = _estimate_messages_tokens,
) -> ColdFoldInput:
    """Fit source into a caller-proved post-prefix/output-room input budget."""
    if (
        isinstance(max_input_tokens, bool)
        or not isinstance(max_input_tokens, int)
        or max_input_tokens <= 0
    ):
        raise ValueError("cold-fold input budget must be a positive integer")
    units = checkpoint_fold_units(source)
    if not units:
        messages = tuple(merge_checkpoint_source_blocks(protected_blocks))
        measured = count_tokens(messages)
        if measured <= max_input_tokens:
            return ColdFoldInput(messages, measured, None, 0)
        raise ColdFoldInputTooLarge("minimum meaningful cold fork input does not fit")
    exact = _candidate(
        source,
        units,
        skipped=0,
        project_tools=False,
        newest_tool_budget_tokens=HARNESS_TOOL_RESULT_BUDGET_TOKENS,
        protected_blocks=protected_blocks,
        count_tokens=count_tokens,
    )
    if exact is not None and exact.input_tokens <= max_input_tokens:
        return exact
    projected = _candidate(
        source,
        units,
        skipped=0,
        project_tools=True,
        newest_tool_budget_tokens=HARNESS_TOOL_RESULT_BUDGET_TOKENS,
        protected_blocks=protected_blocks,
        count_tokens=count_tokens,
    )
    if projected is not None and projected.input_tokens <= max_input_tokens:
        return projected
    turn_starts = tuple(
        index for index in range(1, len(units)) if units[index - 1].completes_turn
    )
    boundary_fit, boundary_skip = _oldest_fitting_candidate(
        source,
        units,
        turn_starts,
        max_input_tokens=max_input_tokens,
        newest_tool_budget_tokens=HARNESS_TOOL_RESULT_BUDGET_TOKENS,
        protected_blocks=protected_blocks,
        count_tokens=count_tokens,
    )
    if boundary_fit is not None and boundary_skip is not None:
        position = turn_starts.index(boundary_skip)
        prior_start = turn_starts[position - 1] if position else 0
        interior_fit, _ = _oldest_fitting_candidate(
            source,
            units,
            range(prior_start + 1, boundary_skip),
            max_input_tokens=max_input_tokens,
            newest_tool_budget_tokens=HARNESS_TOOL_RESULT_BUDGET_TOKENS,
            protected_blocks=protected_blocks,
            count_tokens=count_tokens,
        )
        return interior_fit or boundary_fit
    fitted = _final_turn_fit(
        source,
        units,
        final_start=turn_starts[-1] if turn_starts else 0,
        max_input_tokens=max_input_tokens,
        protected_blocks=protected_blocks,
        count_tokens=count_tokens,
    )
    if fitted is not None:
        return fitted
    raise ColdFoldInputTooLarge("minimum meaningful cold fork input does not fit")
