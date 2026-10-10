"""Validate and select one projected range from durable source Turns."""

from __future__ import annotations

from typing import Any

from history.boundaries import _validate_boundary
from history.source import _NONTERMINAL_TURN_STATES
from history.turns import _project_turn
from history.types import (
    HistoryBoundary,
    PortableHistoryError,
    PortableTurn,
    _SourceTurn,
)


def _validate_excluded_current_input(
    db,
    thread_id: str,
    exclude_message_id: str | None,
) -> None:
    if exclude_message_id is None:
        return
    row = db.execute(
        "SELECT role,delivery_state,active_turn_id,seq FROM messages "
        "WHERE id=? AND thread_id=?",
        (exclude_message_id, thread_id),
    ).fetchone()
    owner = None if row is None or row["active_turn_id"] is None else db.execute(
        "SELECT thread_id,status FROM active_turns WHERE id=?",
        (row["active_turn_id"],),
    ).fetchone()
    if not (
        row is not None
        and row["role"] in {"user", "system"}
        and row["delivery_state"] == "queued"
        and row["seq"] is None
        and owner is not None
        and owner["thread_id"] == thread_id
        and owner["status"] == "running"
    ):
        raise PortableHistoryError(
            "excluded input is not the canonical in-flight Turn input"
        )


def _validate_render_boundaries(
    *,
    thread_id: str,
    epoch: int,
    rows: list[dict[str, Any]],
    source_turns: list[_SourceTurn],
    start_exclusive: HistoryBoundary | None,
    through: HistoryBoundary | None,
) -> None:
    boundaries = {turn.boundary for turn in source_turns if turn.boundary is not None}
    for boundary in (start_exclusive, through):
        if boundary is not None:
            _validate_boundary(
                boundary,
                thread_id=thread_id,
                epoch=epoch,
                rows=rows,
                turn_boundaries=boundaries,
                source_turns=source_turns,
            )
    if (
        start_exclusive is not None
        and through is not None
        and start_exclusive.seq >= through.seq
    ):
        raise PortableHistoryError("history tail boundaries are reversed")


def _selected_source_turns(
    source_turns: list[_SourceTurn],
    *,
    thread_id: str,
    epoch: int,
    lower: int,
    upper: int | None,
) -> list[tuple[_SourceTurn, bool, bool]]:
    """Select exact row ranges while preserving each Turn's identity."""
    selected: list[tuple[_SourceTurn, bool, bool]] = []
    for turn in source_turns:
        if turn.boundary is None:
            continue
        kept = [
            row
            for row in turn.rows
            if int(row["seq"]) > lower and (upper is None or int(row["seq"]) <= upper)
        ]
        if not kept:
            continue
        starts_turn = bool(turn.rows) and kept[0] is turn.rows[0]
        completes_turn = (
            turn.status not in _NONTERMINAL_TURN_STATES
            and (upper is None or turn.boundary.seq <= upper)
        )
        if starts_turn and completes_turn:
            fragment = turn
        else:
            fragment = _SourceTurn(
                turn_id=turn.turn_id,
                status=turn.status,
                legacy_reason=turn.legacy_reason,
                rows=kept,
                last_semantic_source_index=turn.last_semantic_source_index,
                boundary=(
                    turn.boundary
                    if completes_turn
                    else HistoryBoundary(
                        thread_id=thread_id,
                        history_epoch=epoch,
                        seq=int(kept[-1]["seq"]),
                        message_id=str(kept[-1]["id"]),
                    )
                ),
            )
        selected.append((fragment, starts_turn, completes_turn))
    return selected


def _project_selected_turns(
    selected: list[tuple[_SourceTurn, bool, bool]],
    *,
    db,
) -> tuple[PortableTurn, ...]:
    return tuple(
        PortableTurn(
            turn_id=source.turn_id,
            status=source.status,
            messages=_project_turn(source, starts_turn=starts_turn, db=db),
            boundary=source.boundary,
            legacy_reason=source.legacy_reason,
            includes_turn_start=starts_turn,
            includes_turn_end=completes_turn,
        )
        for source, starts_turn, completes_turn in selected
        if source.boundary is not None
    )
