"""Read only the durable rows a checkpoint-scoped history range depends on.

A range that starts after a legal boundary B needs B's owning source segment
(to prove B is still a legal cut and to project B's Turn fragment exactly), the
one semantic row before that segment (Turn order and unsealed-Turn continuity),
and every row after B. Covered history before that segment is not read into
Python: the checkpoint through B already carries it. One SQLite pass over it
answers the two thread-wide facts a seq-0 render derives from it: its retention
stubs, and whether a Turn read here also appears there. Everything that is read
passes the same validators as a seq-0 render.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from core.thread_lifecycle import TOOL_TRIM_MARKER
from history.messages import _turn_provenance
from history.source import (
    _NONTERMINAL_TURN_STATES,
    _SEMANTIC_ROLES,
    SOURCE_COLUMNS,
    validate_source_rows,
)
from history.types import HistoryBoundary, PortableHistoryError


@dataclass(frozen=True)
class ScopedSource:
    """Validated rows from B's owning segment through the head, plus its predecessor."""

    rows: list[dict[str, Any]]
    predecessor: dict[str, Any] | None
    starts_bound_turn: bool

    @property
    def owner_rows(self) -> list[dict[str, Any]]:
        """Rows whose Turn owners the projection must resolve."""
        return [*([self.predecessor] if self.predecessor else []), *self.rows]


def _segment_key(row: dict[str, Any]) -> tuple[str, str] | None:
    """Name the source segment one validated semantic row belongs to."""
    if row["role"] not in _SEMANTIC_ROLES:
        return None
    turn_id, legacy_reason = _turn_provenance(row)
    if turn_id is not None:
        return ("turn", turn_id)
    return ("legacy", str(legacy_reason))


def _anchor(db, thread_id: str, start: HistoryBoundary):
    row = db.execute(
        f"SELECT {SOURCE_COLUMNS} FROM messages WHERE thread_id = ? "
        "AND delivery_state = 'delivered' AND seq = ?",
        (thread_id, start.seq),
    ).fetchone()
    if row is None or str(row["id"]) != start.message_id:
        raise PortableHistoryError(
            "history boundary message does not match durable source"
        )
    return row


def _segment_before(db, thread_id: str, start_seq: int, anchor: dict[str, Any]):
    """Walk back to the first row of the anchor's segment and the row before it.

    A non-semantic anchor sits inside the segment of the nearest semantic row
    before it; a legacy segment begins at its user row.
    """
    key = _segment_key(anchor)
    started = key is not None and key[0] == "legacy" and anchor["role"] == "user"
    earlier: list[Any] = []
    cursor = db.execute(
        f"SELECT {SOURCE_COLUMNS} FROM messages WHERE thread_id = ? "
        "AND delivery_state = 'delivered' AND seq < ? ORDER BY seq DESC",
        (thread_id, start_seq),
    )
    try:
        for source_row in cursor:
            row = validate_source_rows((source_row,), thread_id)[0]
            row_key = _segment_key(row)
            if row_key is not None and key is not None and (started or row_key != key):
                return list(reversed(earlier)), row, key
            if not started:
                earlier.append(source_row)
            if row_key is not None:
                key = key or row_key
                started = started or (key[0] == "legacy" and row["role"] == "user")
    finally:
        cursor.close()
    return list(reversed(earlier)), None, key


def scoped_source(db, thread_id: str, start: HistoryBoundary) -> ScopedSource:
    """Read B's owning segment through the thread head, validated like seq 0."""
    anchor_source = _anchor(db, thread_id, start)
    anchor = validate_source_rows((anchor_source,), thread_id)[0]
    earlier, predecessor, key = _segment_before(db, thread_id, start.seq, anchor)
    later = db.execute(
        f"SELECT {SOURCE_COLUMNS} FROM messages WHERE thread_id = ? "
        "AND delivery_state = 'delivered' AND seq > ? ORDER BY seq",
        (thread_id, start.seq),
    ).fetchall()
    return ScopedSource(
        rows=validate_source_rows((*earlier, anchor_source, *later), thread_id),
        predecessor=predecessor,
        starts_bound_turn=key is not None and key[0] == "turn",
    )


_RETENTION_STUB = "(role = 'tool' AND substr(content, 1, ?) = ?)"
_STUB_MARKER = (len(TOOL_TRIM_MARKER), TOOL_TRIM_MARKER)


def _scope_turn_ids(rows: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(sorted({
        turn_id
        for row in rows
        if row["role"] in _SEMANTIC_ROLES
        for turn_id, _legacy_reason in [_turn_provenance(row)]
        if turn_id is not None
    }))


def _names_scope_turn(turn_ids: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    """SQL for a row whose Turn provenance, as ``_turn_provenance`` reads it, is in scope."""
    if not turn_ids:
        return "0", ()
    marks = ",".join("?" for _ in turn_ids)
    return (
        f"(active_turn_id IN ({marks}) OR (active_turn_id IS NULL AND json_valid(metadata) "
        f"AND json_extract(metadata, '$.turn_id') IN ({marks})))",
        (*turn_ids, *turn_ids),
    )


def _reject_scope_turn(source_row, thread_id: str, turn_ids: tuple[str, ...]) -> None:
    row = validate_source_rows((source_row,), thread_id)[0]
    if row["role"] not in _SEMANTIC_ROLES:
        return
    turn_id, _legacy_reason = _turn_provenance(row)
    if turn_id in turn_ids:
        raise PortableHistoryError(f"Turn {turn_id!r} is interleaved")


def scan_before_scope(db, thread_id: str, source: ScopedSource) -> tuple[int, ...]:
    """Read what a scope needs from the history before it, in two SQLite passes.

    Returns the retention stubs there, by the seq-0 predicate (tool role, content
    starting with the trim marker), so covered content never reaches Python. It
    fails when a Turn the scope reads also has rows there, which seq 0 rejects as
    interleaved or out of order; the seq-0 validators judge each such row.

    The stubs come back as one aggregated value, not a row each: an old thread
    carries thousands of them, and the checkpoint install runs this under the
    writer lock, where every row crossing into Python is a lock-hold step.
    """
    before = source.rows[0]["seq"]
    stub_seqs = db.execute(
        f"SELECT json_group_array(seq) FROM (SELECT seq FROM messages WHERE thread_id = ? "
        f"AND delivery_state = 'delivered' AND seq < ? AND {_RETENTION_STUB} ORDER BY seq)",
        (thread_id, before, *_STUB_MARKER),
    ).fetchone()[0]
    turn_ids = _scope_turn_ids(source.rows)
    if turn_ids:
        names_turn, turn_params = _names_scope_turn(turn_ids)
        cursor = db.execute(
            f"SELECT {SOURCE_COLUMNS} FROM messages WHERE thread_id = ? "
            f"AND delivery_state = 'delivered' AND seq < ? AND {names_turn} ORDER BY seq",
            (thread_id, before, *turn_params),
        )
        try:
            for source_row in cursor:
                _reject_scope_turn(source_row, thread_id, turn_ids)
        finally:
            cursor.close()
    return tuple(sorted(int(seq) for seq in json.loads(stub_seqs)))


def predecessor_turn_order(source: ScopedSource, owners: dict[str, dict]) -> int:
    """Continue Turn-order validation from the Turn that precedes the scope."""
    if source.predecessor is None:
        return 0
    turn_id, _legacy_reason = _turn_provenance(source.predecessor)
    if turn_id is None:
        return 0
    owner = owners[turn_id]
    if owner["status"] in _NONTERMINAL_TURN_STATES:
        later = "sealed" if source.starts_bound_turn else "semantic"
        raise PortableHistoryError(f"unsealed Turn precedes later {later} history")
    return int(owner["turn_order"])


def assistant_step_through(db, thread_id: str, seq: int) -> bool:
    """Whether any delivered Assistant Step exists at or before ``seq``."""
    return db.execute(
        "SELECT 1 FROM messages WHERE thread_id = ? AND delivery_state = 'delivered' "
        "AND seq <= ? AND role = 'assistant' ORDER BY seq LIMIT 1",
        (thread_id, seq),
    ).fetchone() is not None
