"""Validation of legal Assistant-Step-aligned transcript boundaries."""

from __future__ import annotations

from typing import Any

from history.turns import (
    _project_turn,
    assistant_step_batches,
    turn_interior_cut_points,
)
from history.types import (
    HistoryBoundary,
    PortableHistoryError,
    PortableHistoryProjection,
    PortableTurn,
    _SourceTurn,
)


def _legal_slice_boundaries(
    source: PortableHistoryProjection,
) -> set[HistoryBoundary]:
    legal = {
        boundary
        for turn in source.turns
        for boundary in (
            turn.boundary,
            *turn_interior_cut_points(turn),
            *(
                batch.resume_anchor
                for batch in assistant_step_batches(turn)
                if batch.resume_anchor is not None
            ),
        )
    }
    if source.start_exclusive is not None:
        legal.add(source.start_exclusive)
    return legal


def boundary_before_latest_assistant_step(
    source: PortableHistoryProjection,
    *,
    floor: HistoryBoundary | None = None,
    step_precedes_start: bool = False,
) -> HistoryBoundary | None:
    """Return the newest legal cut before the latest provider-visible Step.

    A projection that starts after ``floor`` cannot see Steps before it, so
    the caller states whether one exists; the newest cut before such a Step is
    already covered by the floor.
    """
    if source.start_exclusive is not None and floor != source.start_exclusive:
        raise PortableHistoryError("a scoped projection must be floored at its start")
    newest = next(
        (
            batches[-1]
            for turn in reversed(source.turns)
            if (batches := assistant_step_batches(turn))
        ),
        None,
    )
    if newest is None and step_precedes_start:
        return None
    if newest is None:
        candidate = source.sealed_boundary
    else:
        provider_message_id = next(
            (
                message.provider_message_id
                for message in newest.assistant_and_tools
                if message.role == "assistant" and message.provider_message_id
            ),
            None,
        )
        provider_group = (
            tuple(
                message
                for message in source.messages
                if message.provider_message_id == provider_message_id
            )
            if provider_message_id is not None
            else newest.assistant_and_tools
        )
        first_step_seq = min(message.seq for message in provider_group)
        candidate = max(
            (
                boundary
                for boundary in _legal_slice_boundaries(source)
                if boundary.seq < first_step_seq
            ),
            key=lambda boundary: boundary.seq,
            default=None,
        )
    if candidate is None or (floor is not None and candidate.seq <= floor.seq):
        return None
    return candidate


def _validate_slice_boundary(
    source: PortableHistoryProjection,
    boundary: HistoryBoundary,
    *,
    label: str,
    legal_boundaries: set[HistoryBoundary],
) -> None:
    if boundary.thread_id != source.thread_id:
        raise PortableHistoryError(f"{label} boundary names another thread")
    if boundary.history_epoch != source.history_epoch:
        raise PortableHistoryError(f"{label} boundary epoch is stale")
    if boundary not in legal_boundaries:
        raise PortableHistoryError(
            f"{label} boundary is not a legal cut in the frozen projection"
        )


def _slice_range(
    source: PortableHistoryProjection,
    *,
    start_exclusive: HistoryBoundary | None,
    through: HistoryBoundary | None,
) -> tuple[HistoryBoundary | None, int, int]:
    legal = _legal_slice_boundaries(source)
    effective_start = start_exclusive or source.start_exclusive
    if effective_start is not None:
        _validate_slice_boundary(
            source,
            effective_start,
            label="start",
            legal_boundaries=legal,
        )
    if through is not None:
        _validate_slice_boundary(
            source,
            through,
            label="through",
            legal_boundaries=legal,
        )
    lower = effective_start.seq if effective_start is not None else 0
    upper = (
        through.seq
        if through is not None
        else (
            source.sealed_boundary.seq if source.sealed_boundary is not None else lower
        )
    )
    if lower > upper or (
        through is not None and effective_start is not None and lower == upper
    ):
        raise PortableHistoryError("history tail boundaries are reversed")
    if source.start_exclusive is not None and lower < source.start_exclusive.seq:
        raise PortableHistoryError("slice starts before the frozen projection")
    if source.sealed_boundary is not None and upper > source.sealed_boundary.seq:
        raise PortableHistoryError("slice ends after the frozen projection")
    return effective_start, lower, upper


def _source_turn_interior_cut_points(
    source: _SourceTurn,
) -> tuple[HistoryBoundary, ...]:
    """Enumerate one durable Turn's legal interior cut points."""
    if source.boundary is None:
        return ()
    return turn_interior_cut_points(
        PortableTurn(
            turn_id=source.turn_id,
            status=source.status,
            messages=_project_turn(source),
            boundary=source.boundary,
            legacy_reason=source.legacy_reason,
        )
    )


def _source_turn_resume_anchors(
    source: _SourceTurn,
) -> tuple[HistoryBoundary, ...]:
    """Enumerate durable initiating-user cuts for complete Assistant batches."""
    if source.boundary is None:
        return ()
    turn = PortableTurn(
        turn_id=source.turn_id,
        status=source.status,
        messages=_project_turn(source),
        boundary=source.boundary,
        legacy_reason=source.legacy_reason,
    )
    return tuple(
        batch.resume_anchor
        for batch in assistant_step_batches(turn)
        if batch.resume_anchor is not None
    )


def validate_boundary_identity(
    boundary: HistoryBoundary, *, thread_id: str, epoch: int,
) -> None:
    """Reject a boundary that names another thread or a stale history epoch."""
    if boundary.thread_id != thread_id:
        raise PortableHistoryError("history boundary names another thread")
    if boundary.history_epoch != epoch:
        raise PortableHistoryError("history boundary epoch is stale")


def _validate_boundary(
    boundary: HistoryBoundary,
    *,
    thread_id: str,
    epoch: int,
    rows: list[dict[str, Any]],
    turn_boundaries: set[HistoryBoundary],
    source_turns: list[_SourceTurn],
) -> None:
    """Reject any boundary that is not a legal place to cut this history.

    A legal boundary is a complete Turn boundary, an interior Assistant Step
    end, or the initiating-user resume anchor of one complete Assistant batch.
    This is the single gate for every partial-Turn slice downstream.
    """
    validate_boundary_identity(boundary, thread_id=thread_id, epoch=epoch)
    row = next((row for row in rows if row["seq"] == boundary.seq), None)
    if row is None or str(row["id"]) != boundary.message_id:
        raise PortableHistoryError(
            "history boundary message does not match durable source"
        )
    if boundary in turn_boundaries:
        return
    owner = next(
        (
            turn
            for turn in source_turns
            if turn.boundary is not None
            and any(int(candidate["seq"]) == boundary.seq for candidate in turn.rows)
        ),
        None,
    )
    if owner is None or (
        boundary not in _source_turn_interior_cut_points(owner)
        and boundary not in _source_turn_resume_anchors(owner)
    ):
        raise PortableHistoryError(
            "history boundary is not a complete Turn boundary, legal interior "
            "cut point, or Assistant Step resume anchor"
        )
