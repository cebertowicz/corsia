"""Read and validate the durable transcript source rows."""

from __future__ import annotations

from typing import Any

from core.message_history import is_system_context_event
from history.messages import (
    _metadata,
    _turn_provenance,
)
from history.types import HistoryBoundary, PortableHistoryError, _SourceTurn
from infra.context_preview_timing import record_rows_read


_SEMANTIC_ROLES = frozenset({"user", "assistant", "tool"})
_TERMINAL_TURN_STATES = frozenset({"completed", "cancelled", "interrupted", "error"})
_NONTERMINAL_TURN_STATES = frozenset({"running"})
SOURCE_COLUMNS = "id,thread_id,role,content,metadata,created_at,seq,active_turn_id"
# Below SQLite's oldest default host-parameter limit (999).
_TURN_ID_CHUNK = 500


def thread_history_epoch(db, thread_id: str) -> int:
    """Read one thread's current history epoch, failing when it does not exist."""
    thread_row = db.execute(
        "SELECT history_epoch FROM threads WHERE id = ?",
        (thread_id,),
    ).fetchone()
    record_rows_read(int(thread_row is not None))
    if thread_row is None:
        raise PortableHistoryError(f"thread {thread_id!r} does not exist")
    return int(thread_row["history_epoch"])


def _validated_rows(
    db,
    thread_id: str,
) -> tuple[int, list[dict[str, Any]]]:
    epoch = thread_history_epoch(db, thread_id)
    source = db.execute(
        f"SELECT {SOURCE_COLUMNS} "
        "FROM messages WHERE thread_id = ? AND delivery_state = 'delivered' "
        "ORDER BY seq",
        (thread_id,),
    ).fetchall()
    record_rows_read(len(source))
    return epoch, validate_source_rows(source, thread_id)


def validate_source_rows(source, thread_id: str) -> list[dict[str, Any]]:
    """Validate delivered rows in ``seq`` order and attach their parsed metadata."""
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    previous = 0
    for source_row in source:
        row = dict(source_row)
        seq = row.get("seq")
        if (
            not isinstance(seq, int)
            or isinstance(seq, bool)
            or seq <= 0
            or seq in seen
            or seq <= previous
        ):
            raise PortableHistoryError(
                f"thread {thread_id!r} has invalid message sequence {seq!r}"
            )
        if row.get("thread_id") != thread_id:
            raise PortableHistoryError("portable row belongs to another thread")
        seen.add(seq)
        previous = seq
        role = row.get("role")
        if role not in _SEMANTIC_ROLES and role != "system":
            raise PortableHistoryError(
                f"message {row.get('id')!r} has unsupported role {role!r}"
            )
        if is_system_context_event(row):
            # The durable role owns visible attribution. Portable replay owns
            # provider semantics: a wake starts a Turn exactly like the live
            # event dispatch that consumed it as inbound input.
            row["role"] = role = "user"
        row["_meta"] = _metadata(
            row.get("metadata"),
            message_id=str(row.get("id")),
            role=str(role),
        )
        rows.append(row)
    return rows


def _turn_owners(db, thread_id: str, rows: list[dict[str, Any]]) -> dict[str, dict]:
    turn_ids = sorted(
        {
            turn_id
            for row in rows
            if row.get("role") in _SEMANTIC_ROLES
            and row.get("_exclude_current") is not True
            for turn_id, _legacy_reason in [_turn_provenance(row)]
            if turn_id is not None
        }
    )
    if not turn_ids:
        return {}
    # Read only the named Turns: a whole-thread read grows with thread lifetime,
    # and every fetched row is a GIL re-acquire on the render path.
    owners: dict[str, dict] = {}
    for start in range(0, len(turn_ids), _TURN_ID_CHUNK):
        chunk = turn_ids[start:start + _TURN_ID_CHUNK]
        fetched = db.execute(
            "SELECT id,thread_id,status,turn_order FROM active_turns "
            f"WHERE thread_id = ? AND id IN ({','.join('?' for _ in chunk)})",
            (thread_id, *chunk),
        ).fetchall()
        record_rows_read(len(fetched))
        owners.update((str(row["id"]), dict(row)) for row in fetched)
    for turn_id in turn_ids:
        owner = owners.get(turn_id)
        if owner is None:
            raise PortableHistoryError(f"message names missing Turn {turn_id!r}")
        if owner.get("thread_id") != thread_id:
            raise PortableHistoryError(f"Turn {turn_id!r} belongs to another thread")
        status = owner.get("status")
        if status not in _NONTERMINAL_TURN_STATES | _TERMINAL_TURN_STATES:
            raise PortableHistoryError(
                f"Turn {turn_id!r} has unsupported status {status!r}"
            )
    return owners


def _append_legacy_row(
    turns: list[_SourceTurn],
    current: _SourceTurn | None,
    row: dict[str, Any],
    *,
    source_index: int,
    legacy_reason: str | None,
    active_start: int | None,
) -> _SourceTurn:
    if active_start is not None:
        raise PortableHistoryError("unsealed Turn precedes later semantic history")
    starts_segment = (
        current is None
        or current.turn_id is not None
        or row["role"] == "user"
        or current.legacy_reason != legacy_reason
    )
    if starts_segment:
        # Old task/work-stream threads may start with assistant output because
        # their initiating task lived outside message history. Only a leading
        # tool result is structurally ownerless.
        if row["role"] == "tool":
            raise PortableHistoryError(
                "legacy segment starts with an ownerless tool result"
            )
        current = _SourceTurn(
            turn_id=None,
            status="legacy",
            legacy_reason=str(legacy_reason),
            rows=[],
            last_semantic_source_index=source_index,
        )
        turns.append(current)
    current.last_semantic_source_index = source_index
    current.rows.append(row)
    return current


def _flush_pending_unbound_rows(
    turns: list[_SourceTurn],
    current: _SourceTurn | None,
    pending_rows: list[tuple[dict[str, Any], int, str | None]],
    *,
    active_start: int | None,
) -> _SourceTurn | None:
    """Append an unbound run that cannot stay in the current bound Turn."""
    for row, source_index, legacy_reason in pending_rows:
        current = _append_legacy_row(
            turns,
            current,
            row,
            source_index=source_index,
            legacy_reason=legacy_reason,
            active_start=active_start,
        )
    pending_rows.clear()
    return current


def _append_bound_row(
    turns: list[_SourceTurn],
    current: _SourceTurn | None,
    row: dict[str, Any],
    *,
    source_index: int,
    turn_id: str,
    owner: dict[str, Any],
    seen_bound: set[str],
    active_start: int | None,
    last_turn_order: int,
) -> tuple[_SourceTurn, int | None, int]:
    status = str(owner["status"])
    if current is None or current.turn_id != turn_id:
        if turn_id in seen_bound:
            raise PortableHistoryError(f"Turn {turn_id!r} is interleaved")
        if active_start is not None:
            raise PortableHistoryError("unsealed Turn precedes later sealed history")
        turn_order = int(owner["turn_order"])
        if turn_order <= last_turn_order:
            raise PortableHistoryError("Turn order conflicts with transcript sequence")
        last_turn_order = turn_order
        seen_bound.add(turn_id)
        current = _SourceTurn(
            turn_id=turn_id,
            status=status,
            legacy_reason=None,
            rows=[],
            last_semantic_source_index=source_index,
        )
        if status in _NONTERMINAL_TURN_STATES:
            active_start = source_index
        else:
            turns.append(current)
    elif status != current.status:
        raise PortableHistoryError(f"Turn {turn_id!r} status changed during projection")
    if status != "active":
        current.last_semantic_source_index = source_index
        current.rows.append(row)
    return current, active_start, last_turn_order


def _source_turns(
    thread_id: str,
    epoch: int,
    rows: list[dict[str, Any]],
    owners: dict[str, dict],
    *,
    prior_turn_order: int = 0,
) -> tuple[list[_SourceTurn], int | None]:
    turns: list[_SourceTurn] = []
    current: _SourceTurn | None = None
    seen_bound: set[str] = set()
    active_start: int | None = None
    last_turn_order = prior_turn_order
    pending_unbound: list[tuple[dict[str, Any], int, str | None]] = []

    for source_index, row in enumerate(rows):
        if row.get("role") not in _SEMANTIC_ROLES:
            continue
        if row.get("_exclude_current") is True:
            if row.get("role") != "user" or active_start is not None:
                raise PortableHistoryError(
                    "excluded in-flight message is not one current user input"
                )
            active_start = source_index
            current = None
            continue
        turn_id, legacy_reason = _turn_provenance(row)
        if turn_id is None:
            pending_unbound.append((row, source_index, legacy_reason))
            continue
        if pending_unbound:
            if current is not None and current.turn_id == turn_id:
                for pending_row, pending_index, _pending_reason in pending_unbound:
                    current.last_semantic_source_index = pending_index
                    current.rows.append(pending_row)
                pending_unbound.clear()
            else:
                current = _flush_pending_unbound_rows(
                    turns,
                    current,
                    pending_unbound,
                    active_start=active_start,
                )
        current, active_start, last_turn_order = _append_bound_row(
            turns,
            current,
            row,
            source_index=source_index,
            turn_id=turn_id,
            owner=owners[turn_id],
            seen_bound=seen_bound,
            active_start=active_start,
            last_turn_order=last_turn_order,
        )

    current = _flush_pending_unbound_rows(
        turns,
        current,
        pending_unbound,
        active_start=active_start,
    )

    if active_start is not None and current is not None:
        turns.append(current)
    # A final active Turn is the expected current suffix. Callers decide
    # whether their frozen projection includes that durable prefix.
    for turn in turns:
        boundary_row = rows[turn.last_semantic_source_index]
        turn.boundary = HistoryBoundary(
            thread_id=thread_id,
            history_epoch=epoch,
            seq=int(boundary_row["seq"]),
            message_id=str(boundary_row["id"]),
        )
    return turns, active_start
