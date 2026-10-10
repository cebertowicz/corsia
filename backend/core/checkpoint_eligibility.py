"""The one rule for whether a manual checkpoint can currently be admitted.

Shared by the checkpoint-start endpoint (`api/checkpoints.py`) and the usage/
preview payload (`core/context_preview.py`) so the sheet only ever offers
Checkpoint Now when a click would actually be admitted (#1952).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from history.projection import render_portable_history
from history.types import HistoryBoundary


@dataclass(frozen=True, slots=True)
class CheckpointEligibility:
    eligible: bool
    reason_key: str | None
    thread_row: Any
    previous: Any | None
    boundary: Any | None
    target: Any | None
    projection: Any | None


@dataclass(frozen=True, slots=True)
class CheckpointEligibilityProjection:
    """A same-boundary history projection already rendered by a caller."""

    start_exclusive: Any | None
    sealed_boundary: Any | None


def checkpoint_eligibility(
    thread_id: str, user_id: str, *, db, thread_row: Any | None = None,
    rendered_projection: CheckpointEligibilityProjection | None = None,
) -> CheckpointEligibility | None:
    """Return None when the thread is not this user's; else the eligibility.

    ``thread_row`` may be an already-fetched thread row for this exact user, to
    avoid a second ownership lookup when the caller already validated one.
    """

    from infra.checkpoint_queries import current_installed
    from infra.queries import get_thread_for_user
    from llm.resolution import resolve_execution_target

    if thread_row is None:
        thread_row = get_thread_for_user(thread_id, user_id, db=db)
    if thread_row is None:
        return None
    previous = current_installed(thread_id, db=db)
    start_exclusive = HistoryBoundary.of_checkpoint(previous) if previous else None
    if (
        rendered_projection is not None
        and rendered_projection.start_exclusive == start_exclusive
    ):
        projection = rendered_projection
    else:
        projection = render_portable_history(
            thread_id, db=db, start_exclusive=start_exclusive,
        )
    boundary = projection.sealed_boundary
    if boundary is None:
        return CheckpointEligibility(
            False, "no_sealed_boundary", thread_row, previous, boundary, None, projection,
        )
    target = resolve_execution_target(str(thread_row["model"] or ""), db=db)
    if not target.available:
        return CheckpointEligibility(
            False, "model_unavailable", thread_row, previous, boundary, target, projection,
        )
    if previous is not None and boundary.seq <= previous.through_seq:
        return CheckpointEligibility(
            False, "no_new_history", thread_row, previous, boundary, target, projection,
        )
    return CheckpointEligibility(
        True, None, thread_row, previous, boundary, target, projection,
    )
