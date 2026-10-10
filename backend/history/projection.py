"""Canonical read-only projection of CORSIA's durable transcript."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable
from typing import Any, NamedTuple, TypeVar

from core.thread_lifecycle import TOOL_TRIM_MARKER
from history.boundaries import _slice_range, validate_boundary_identity
from history.scoped_source import (
    predecessor_turn_order,
    scan_before_scope,
    scoped_source,
)
from history.selection import (
    _project_selected_turns,
    _selected_source_turns,
    _validate_excluded_current_input,
    _validate_render_boundaries,
)
from history.source import (
    _NONTERMINAL_TURN_STATES,
    _source_turns,
    _turn_owners,
    _validated_rows,
    thread_history_epoch,
)
from history.types import (
    PORTABLE_HISTORY_FORMAT_VERSION,
    HistoryBoundary,
    PortableHistoryError,
    PortableHistoryProjection,
    PortableHistoryUnavailableError,
    PortableTurn,
    _SourceTurn,
)

_T = TypeVar("_T")


class _Source(NamedTuple):
    """Validated source Turns for one render and the facts derived from them."""

    epoch: int
    rows: list[dict[str, Any]]
    turns: list[_SourceTurn]
    retention_stub_seqs: tuple[int, ...]
    predecessor: dict[str, Any] | None


def _retention_stubs(rows: list[dict[str, Any]]) -> tuple[int, ...]:
    """Name the tool rows whose payload retention trimmed, in ``seq`` order."""
    return tuple(
        int(row["seq"])
        for row in rows
        if row["role"] == "tool"
        and str(row.get("content") or "").startswith(TOOL_TRIM_MARKER)
    )


def _read_source(
    db,
    thread_id: str,
    start_exclusive: HistoryBoundary | None,
) -> _Source:
    """Read seq-0 history, or only what a range after a legal boundary needs."""
    if start_exclusive is None:
        epoch, rows = _validated_rows(db, thread_id)
        owners = _turn_owners(db, thread_id, rows)
        source_turns, _active_start = _source_turns(thread_id, epoch, rows, owners)
        return _Source(epoch, rows, source_turns, _retention_stubs(rows), None)
    epoch = thread_history_epoch(db, thread_id)
    validate_boundary_identity(start_exclusive, thread_id=thread_id, epoch=epoch)
    scoped = scoped_source(db, thread_id, start_exclusive)
    owners = _turn_owners(db, thread_id, scoped.owner_rows)
    source_turns, _active_start = _source_turns(
        thread_id, epoch, scoped.rows, owners,
        prior_turn_order=predecessor_turn_order(scoped, owners),
    )
    stubs = (*scan_before_scope(db, thread_id, scoped), *_retention_stubs(scoped.rows))
    return _Source(epoch, scoped.rows, source_turns, stubs, scoped.predecessor)


def _project_source(
    thread_id: str,
    source: _Source,
    *,
    db,
    start_exclusive: HistoryBoundary | None,
    through: HistoryBoundary | None,
    include_unfinished_turn: bool,
) -> PortableHistoryProjection:
    source_turns = source.turns
    if not include_unfinished_turn:
        source_turns = [
            turn for turn in source_turns
            if turn.status not in _NONTERMINAL_TURN_STATES
        ]
    _validate_render_boundaries(
        thread_id=thread_id,
        epoch=source.epoch,
        rows=source.rows,
        source_turns=source_turns,
        start_exclusive=start_exclusive,
        through=through,
    )
    lower = start_exclusive.seq if start_exclusive is not None else 0
    upper = through.seq if through is not None else None
    selected = _selected_source_turns(
        source_turns,
        thread_id=thread_id,
        epoch=source.epoch,
        lower=lower,
        upper=upper,
    )
    sealed_boundary = selected[-1][0].boundary if selected else start_exclusive
    if through is not None and sealed_boundary != through:
        raise PortableHistoryError("requested sealed boundary was not fully selected")

    if start_exclusive is not None and any(
        seq > start_exclusive.seq
        and (sealed_boundary is None or seq <= sealed_boundary.seq)
        for seq in source.retention_stub_seqs
    ):
        raise PortableHistoryUnavailableError(
            "requested exact tail contains a retained tool-output stub"
        )

    projected_turns = _project_selected_turns(selected, db=db)
    # One entry per LOST CALL, not per distinct seq: every stub in a Step is
    # anchored at that Step's own seq, so deduplicating would report a batch
    # that lost twelve results as one. The notice states a count.
    missing_result_stub_seqs = tuple(
        sorted(
            message.seq
            for turn in projected_turns
            for message in turn.messages
            if message.synthetic_reason == "tool_result_missing"
        )
    )
    return PortableHistoryProjection(
        format_version=PORTABLE_HISTORY_FORMAT_VERSION,
        thread_id=thread_id,
        history_epoch=source.epoch,
        start_exclusive=start_exclusive,
        sealed_boundary=sealed_boundary,
        turns=projected_turns,
        full_raw_replay_available=not source.retention_stub_seqs,
        retention_stub_seqs=source.retention_stub_seqs,
        missing_result_stub_seqs=missing_result_stub_seqs,
    )


def _render_portable_history(
    thread_id: str,
    *,
    db,
    start_exclusive: HistoryBoundary | None = None,
    through: HistoryBoundary | None = None,
    exclude_message_id: str | None = None,
    include_unfinished_turn: bool = False,
) -> PortableHistoryProjection:
    _validate_excluded_current_input(db, thread_id, exclude_message_id)
    source = _read_source(db, thread_id, start_exclusive)
    return _project_source(
        thread_id,
        source,
        db=db,
        start_exclusive=start_exclusive,
        through=through,
        include_unfinished_turn=include_unfinished_turn,
    )


def in_read_snapshot(db, read: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """Run one history read inside a single SQLite savepoint."""
    savepoint = "portable_history_read_snapshot"
    db.execute(f"SAVEPOINT {savepoint}")
    try:
        result = read(*args, db=db, **kwargs)
        db.execute(f"RELEASE {savepoint}")
        return result
    except Exception:
        db.execute(f"ROLLBACK TO {savepoint}")
        db.execute(f"RELEASE {savepoint}")
        raise


def render_portable_history(
    thread_id: str,
    *,
    db,
    start_exclusive: HistoryBoundary | None = None,
    through: HistoryBoundary | None = None,
    exclude_message_id: str | None = None,
    include_unfinished_turn: bool = False,
) -> PortableHistoryProjection:
    """Render one exact range from a single SQLite read snapshot."""
    return in_read_snapshot(
        db,
        _render_portable_history,
        thread_id,
        start_exclusive=start_exclusive,
        through=through,
        exclude_message_id=exclude_message_id,
        include_unfinished_turn=include_unfinished_turn,
    )


def _exact_sealed_boundary(thread_id: str, source: _Source) -> HistoryBoundary | None:
    """The sealed boundary a seq-0 exact replay of the same snapshot carries.

    That boundary ends the newest terminal source Turn. When no terminal Turn
    reaches the scoped rows, it ends the segment just before them.
    """
    terminal = [
        turn for turn in source.turns
        if turn.status not in _NONTERMINAL_TURN_STATES and turn.boundary is not None
    ]
    if terminal:
        return terminal[-1].boundary
    if source.predecessor is None:
        return None
    return HistoryBoundary(
        thread_id=thread_id,
        history_epoch=source.epoch,
        seq=int(source.predecessor["seq"]),
        message_id=str(source.predecessor["id"]),
    )


def _render_checkpoint_tail(
    thread_id: str,
    *,
    db,
    start_exclusive: HistoryBoundary,
    exclude_message_id: str | None,
) -> tuple[PortableHistoryProjection, HistoryBoundary | None]:
    _validate_excluded_current_input(db, thread_id, exclude_message_id)
    source = _read_source(db, thread_id, start_exclusive)
    tail = _project_source(
        thread_id, source, db=db, start_exclusive=start_exclusive, through=None,
        include_unfinished_turn=True,
    )
    return tail, _exact_sealed_boundary(thread_id, source)


def render_checkpoint_tail(
    thread_id: str,
    *,
    db,
    start_exclusive: HistoryBoundary,
    exclude_message_id: str | None = None,
) -> tuple[PortableHistoryProjection, HistoryBoundary | None]:
    """Render a checkpoint's unfinished-inclusive tail and the exact replay's seal.

    Both come from one snapshot without reading history the checkpoint covers,
    so a caller can defer the exact replay itself and still know its identity.
    """
    return in_read_snapshot(
        db,
        _render_checkpoint_tail,
        thread_id,
        start_exclusive=start_exclusive,
        exclude_message_id=exclude_message_id,
    )


def _slice_turns(
    source: PortableHistoryProjection,
    *,
    through: HistoryBoundary | None,
    lower: int,
    upper: int,
) -> list[PortableTurn]:
    selected: list[PortableTurn] = []
    for turn in source.turns:
        kept = tuple(
            message for message in turn.messages if lower < message.seq <= upper
        )
        if not kept:
            continue
        through_in_turn = through is not None and any(
            message.seq == through.seq and message.message_id == through.message_id
            for message in turn.messages
        )
        selected_boundary = through if through_in_turn else turn.boundary
        selected.append(
            PortableTurn(
                turn_id=turn.turn_id,
                status=turn.status,
                messages=kept,
                boundary=selected_boundary,
                legacy_reason=turn.legacy_reason,
                includes_turn_start=(
                    turn.includes_turn_start and kept[0] is turn.messages[0]
                ),
                includes_turn_end=(
                    turn.includes_turn_end and selected_boundary == turn.boundary
                ),
            )
        )
    return selected


def slice_portable_projection(
    source: PortableHistoryProjection,
    *,
    start_exclusive: HistoryBoundary | None = None,
    through: HistoryBoundary | None = None,
) -> PortableHistoryProjection:
    """Slice one already-frozen projection at the shared legal cut kinds.

    This is the in-memory counterpart to :func:`render_portable_history`.
    Callers that derive a boundary from `source` must use this function rather
    than open a second database snapshot whose rows may no longer match that
    boundary.
    """

    effective_start, lower, upper = _slice_range(
        source,
        start_exclusive=start_exclusive,
        through=through,
    )
    selected = _slice_turns(
        source,
        through=through,
        lower=lower,
        upper=upper,
    )

    sealed = selected[-1].boundary if selected else effective_start
    if through is not None and sealed != through:
        raise PortableHistoryError(
            "requested boundary was not selected from the frozen projection"
        )
    first_stub = bisect_right(source.retention_stub_seqs, lower)
    after_last_stub = bisect_right(source.retention_stub_seqs, upper)
    first_missing = bisect_right(source.missing_result_stub_seqs, lower)
    after_last_missing = bisect_right(source.missing_result_stub_seqs, upper)
    return PortableHistoryProjection(
        format_version=source.format_version,
        thread_id=source.thread_id,
        history_epoch=source.history_epoch,
        start_exclusive=effective_start,
        sealed_boundary=sealed,
        turns=tuple(selected),
        full_raw_replay_available=source.full_raw_replay_available,
        retention_stub_seqs=source.retention_stub_seqs[first_stub:after_last_stub],
        missing_result_stub_seqs=source.missing_result_stub_seqs[
            first_missing:after_last_missing
        ],
        resume_presence_line=source.resume_presence_line,
    )
