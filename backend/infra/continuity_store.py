"""Durable continuity entries: the checkpoint's list state, addressable by id.

A checkpoint today is rewritten whole on every fold. That is slow, and it is why
entries are *forgotten* rather than removed: nothing is addressable, so omission
is indistinguishable from deletion.

This module gives every entry a small integer id that is stable for the life of
a history epoch, so an agent can say "drop 7" and mean it. Removal becomes an
act with a reason attached instead of a silence.

Two tables, deliberately:

* ``continuity_entries`` — one row per entry, tombstoned rather than deleted so
  a retirement keeps its reason and its evidence.
* ``continuity_store`` — one row per (thread, epoch) holding the revision the
  fold compares against, and the id allocator. Both facts change together on
  every write, so they share a row rather than a second mechanism.

Entry bodies use one continuity value contract everywhere they are written.

Retention follows the transcript: cascade on thread delete, explicit delete on
lifecycle purge beside messages and checkpoint state.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from core.continuity_types import (
    ENTRY_FIELD_MODELS,
    ENTRY_FIELDS,
    EVIDENCE_REQUIRED_FIELDS,
    MAX_LIST_ITEMS,
)


class ContinuityError(Exception):
    """A continuity edit was refused."""


class ContinuityConflict(ContinuityError):
    """The store moved since the caller read it."""


def check_field_match(field: str, entry_id: int, actual_field: str) -> None:
    """Refuse a caller-supplied ``field`` that disagrees with the entry's own.

    ``field`` is optional on replace/drop/inspect-by-id: the entry already
    carries it. When a caller supplies one anyway, it must agree, or they are
    looking at the wrong entry (#1653).
    """
    if field != actual_field:
        raise ContinuityError(
            f"field {field} does not match entry {entry_id}'s field {actual_field}"
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_entry_body(field: str, body: Any) -> str:
    """Validate one entry against the checkpoint schema and return its JSON.

    Reusing the checkpoint models is the point: a continuity entry is a
    checkpoint entry, so there is exactly one definition of a legal one.
    """
    model = ENTRY_FIELD_MODELS.get(field)
    if model is None:
        raise ContinuityError(f"unknown continuity field: {field!r}")
    try:
        validated = model.model_validate(body, strict=True)
    except Exception as exc:
        raise ContinuityError(f"invalid {field} entry: {exc}") from exc
    return json.dumps(
        validated.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _store_row(conn: sqlite3.Connection, thread_id: str, epoch: int):
    return conn.execute(
        "SELECT revision, next_entry_id FROM continuity_store "
        "WHERE thread_id=? AND history_epoch=?",
        (thread_id, epoch),
    ).fetchone()


def _bump_store(
    conn: sqlite3.Connection,
    thread_id: str,
    epoch: int,
    *,
    consume_id: bool = False,
) -> tuple[int, int]:
    """Advance the store revision, optionally allocating one entry id."""
    now = _now()
    conn.execute(
        "INSERT OR IGNORE INTO continuity_store "
        "(thread_id,history_epoch,revision,next_entry_id,updated_at) "
        "VALUES (?,?,0,1,?)",
        (thread_id, epoch, now),
    )
    row = conn.execute(
        "UPDATE continuity_store SET revision=revision+1,"
        "next_entry_id=next_entry_id+?,updated_at=? "
        "WHERE thread_id=? AND history_epoch=? "
        "RETURNING revision,next_entry_id-? AS allocated_id",
        (int(consume_id), now, thread_id, epoch, int(consume_id)),
    ).fetchone()
    if row is None:
        raise ContinuityError("continuity store allocation failed")
    return int(row["revision"]), int(row["allocated_id"])


def store_revision(conn: sqlite3.Connection, thread_id: str, epoch: int) -> int:
    """The revision a fold patch must be based on to be accepted."""
    row = _store_row(conn, thread_id, epoch)
    return 0 if row is None else int(row["revision"])


def reserve_entry_id(
    conn: sqlite3.Connection, thread_id: str, history_epoch: int
) -> int:
    """Reserve one real id without changing live continuity."""
    now = _now()
    conn.execute(
        "INSERT OR IGNORE INTO continuity_store "
        "(thread_id,history_epoch,revision,next_entry_id,updated_at) "
        "VALUES (?,?,0,1,?)",
        (thread_id, history_epoch, now),
    )
    row = conn.execute(
        "UPDATE continuity_store SET next_entry_id=next_entry_id+1,updated_at=? "
        "WHERE thread_id=? AND history_epoch=? RETURNING next_entry_id-1",
        (now, thread_id, history_epoch),
    ).fetchone()
    if row is None:
        raise ContinuityError("continuity id reservation failed")
    return int(row[0])


def _active_count(
    conn: sqlite3.Connection, thread_id: str, epoch: int, field: str
) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM continuity_entries "
            "WHERE thread_id=? AND history_epoch=? AND field=? AND state='active'",
            (thread_id, epoch, field),
        ).fetchone()[0]
    )


def add_entry(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    history_epoch: int,
    field: str,
    body: Any,
) -> int:
    """Record one new continuity entry and return its addressable id."""
    encoded = validate_entry_body(field, body)
    if _active_count(conn, thread_id, history_epoch, field) >= MAX_LIST_ITEMS:
        # Loud, because the alternative is a checkpoint that silently fails
        # validation later, at the worst possible moment.
        raise ContinuityError(
            f"{field} already holds the maximum {MAX_LIST_ITEMS} active entries"
        )
    revision, entry_id = _bump_store(conn, thread_id, history_epoch, consume_id=True)
    now = _now()
    conn.execute(
        "INSERT INTO continuity_entries "
        "(thread_id, history_epoch, entry_id, field, body, revision, state, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?, 'active', ?,?)",
        (thread_id, history_epoch, entry_id, field, encoded, revision, now, now),
    )
    return entry_id


def add_reserved_entry(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    history_epoch: int,
    entry_id: int,
    field: str,
    body: Any,
) -> None:
    """Activate an id that a running checkpoint already reserved."""
    encoded = validate_entry_body(field, body)
    if _active_count(conn, thread_id, history_epoch, field) >= MAX_LIST_ITEMS:
        raise ContinuityConflict(f"{field} changed while the checkpoint ran")
    existing = conn.execute(
        "SELECT 1 FROM continuity_entries WHERE thread_id=? "
        "AND history_epoch=? AND entry_id=?",
        (thread_id, history_epoch, entry_id),
    ).fetchone()
    if existing is not None:
        raise ContinuityConflict(
            f"continuity entry {entry_id} changed while the checkpoint ran"
        )
    revision, _ = _bump_store(conn, thread_id, history_epoch)
    now = _now()
    conn.execute(
        "INSERT INTO continuity_entries "
        "(thread_id,history_epoch,entry_id,field,body,revision,state,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,'active',?,?)",
        (
            thread_id,
            history_epoch,
            entry_id,
            field,
            encoded,
            revision,
            now,
            now,
        ),
    )


def replace_entry(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    history_epoch: int,
    entry_id: int,
    body: Any,
    expected_revision: int | None = None,
    expected_field: str | None = None,
) -> None:
    """Rewrite one entry in place, keeping its id and its history.

    The entry's own field decides which body shape is legal. The caller does
    not have to supply it: ``entry_id`` already identifies the entry, and
    asking an agent to repeat a fact the store can read would add a
    remembered input for nothing. When a caller supplies ``expected_field``
    anyway (a redundant but correct confirmation, #1653), it must match.
    """
    row = conn.execute(
        "SELECT field, revision, state FROM continuity_entries "
        "WHERE thread_id=? AND history_epoch=? AND entry_id=?",
        (thread_id, history_epoch, entry_id),
    ).fetchone()
    if row is None:
        raise ContinuityError(f"continuity entry {entry_id} does not exist")
    if expected_field is not None:
        check_field_match(expected_field, entry_id, str(row["field"]))
    if row["state"] != "active":
        raise ContinuityError(f"continuity entry {entry_id} is retired")
    if expected_revision is not None and int(row["revision"]) != expected_revision:
        raise ContinuityConflict(
            f"continuity entry {entry_id} changed since it was read"
        )
    encoded = validate_entry_body(str(row["field"]), body)
    revision, _ = _bump_store(conn, thread_id, history_epoch)
    conn.execute(
        "UPDATE continuity_entries SET body=?, revision=?, updated_at=? "
        "WHERE thread_id=? AND history_epoch=? AND entry_id=?",
        (encoded, revision, _now(), thread_id, history_epoch, entry_id),
    )


def validate_retirement(
    field: str, reason: str, source_ref: str | None
) -> tuple[str, str | None]:
    """Validate one explicit retirement and return normalized evidence."""
    cleaned = (reason or "").strip()
    source = (source_ref or "").strip() or None
    if not cleaned:
        raise ContinuityError("retiring a continuity entry requires a reason")
    if field in EVIDENCE_REQUIRED_FIELDS and source is None:
        raise ContinuityError(
            f"retiring a {field} entry requires source_ref naming the evidence"
        )
    return cleaned, source


def retire_entry(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    history_epoch: int,
    entry_id: int,
    reason: str,
    source_ref: str | None = None,
    expected_field: str | None = None,
) -> None:
    """Retire one entry, with the reason it stopped applying.

    Tombstoned, never deleted: a retirement that leaves no trace is the silent
    drop this whole mechanism exists to prevent. When a caller supplies
    ``expected_field`` (a redundant but correct confirmation, #1653), it must
    match the entry's own field.
    """
    row = conn.execute(
        "SELECT field, state FROM continuity_entries "
        "WHERE thread_id=? AND history_epoch=? AND entry_id=?",
        (thread_id, history_epoch, entry_id),
    ).fetchone()
    if row is None:
        raise ContinuityError(f"continuity entry {entry_id} does not exist")
    if expected_field is not None:
        check_field_match(expected_field, entry_id, str(row["field"]))
    if row["state"] != "active":
        return  # Retiring twice is the same outcome; do not fail a replay.
    field = str(row["field"])
    cleaned, source = validate_retirement(field, reason, source_ref)
    revision, _ = _bump_store(conn, thread_id, history_epoch)
    conn.execute(
        "UPDATE continuity_entries SET state='retired', retired_reason=?, "
        "retired_source_ref=?, revision=?, updated_at=? "
        "WHERE thread_id=? AND history_epoch=? AND entry_id=?",
        (
            cleaned,
            source,
            revision,
            _now(),
            thread_id,
            history_epoch,
            entry_id,
        ),
    )


def active_entries(
    conn: sqlite3.Connection, thread_id: str, history_epoch: int
) -> dict[str, list[dict[str, Any]]]:
    """The current continuity state, shaped like the checkpoint's list fields."""
    rows = conn.execute(
        "SELECT entry_id, field, body FROM continuity_entries "
        "WHERE thread_id=? AND history_epoch=? AND state='active' "
        "ORDER BY field, entry_id",
        (thread_id, history_epoch),
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {field: [] for field in ENTRY_FIELDS}
    for row in rows:
        grouped[str(row["field"])].append(
            {"entry_id": int(row["entry_id"]), **json.loads(str(row["body"]))}
        )
    return grouped


def active_entry_snapshot(
    conn: sqlite3.Connection, thread_id: str, history_epoch: int
) -> list[dict[str, Any]]:
    """Return deterministic active bodies with their merge revisions."""
    rows = conn.execute(
        "SELECT entry_id,field,body,revision FROM continuity_entries "
        "WHERE thread_id=? AND history_epoch=? AND state='active' "
        "ORDER BY entry_id",
        (thread_id, history_epoch),
    ).fetchall()
    return [
        {
            "entry_id": int(row["entry_id"]),
            "field": str(row["field"]),
            "body": json.loads(str(row["body"])),
            "revision": int(row["revision"]),
        }
        for row in rows
    ]


def delete_thread_continuity(conn: sqlite3.Connection, thread_id: str) -> None:
    """Drop a thread's continuity when its transcript is destroyed.

    Purge keeps the session row, so the cascade never fires there.
    """
    conn.execute("DELETE FROM lesson_placement_checks WHERE thread_id=?", (thread_id,))
    conn.execute("DELETE FROM continuity_entries WHERE thread_id=?", (thread_id,))
    conn.execute("DELETE FROM continuity_store WHERE thread_id=?", (thread_id,))
