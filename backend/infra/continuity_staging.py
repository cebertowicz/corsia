"""The one disposable-checkpoint seam for continuity edits."""

from __future__ import annotations

import functools
import json
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from core.continuity_types import ENTRY_FIELDS, MAX_LIST_ITEMS
from infra.continuity_store import (
    ContinuityConflict,
    ContinuityError,
    active_entry_snapshot,
    add_reserved_entry,
    check_field_match,
    replace_entry,
    reserve_entry_id,
    retire_entry,
    store_revision,
    validate_entry_body,
    validate_retirement,
)


STAGING_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS checkpoint_staged_edits (
    checkpoint_id TEXT NOT NULL REFERENCES checkpoints(id) ON DELETE CASCADE,
    edit_order INTEGER NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('add','replace','drop','review')),
    entry_id INTEGER NOT NULL CHECK (entry_id > 0),
    field TEXT NOT NULL,
    body TEXT CHECK (body IS NULL OR json_valid(body)),
    reason TEXT,
    source_ref TEXT,
    base_entry_revision INTEGER,
    outcome TEXT NOT NULL DEFAULT 'pending'
        CHECK (outcome IN ('pending','applied','conflict')),
    outcome_detail TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT,
    PRIMARY KEY (checkpoint_id, edit_order)
);
CREATE INDEX IF NOT EXISTS checkpoint_staged_edits_target
    ON checkpoint_staged_edits(checkpoint_id,entry_id,edit_order);
"""

_current_checkpoint_id: ContextVar[str | None] = ContextVar(
    "checkpoint_execution_id", default=None
)


@contextmanager
def checkpoint_execution_scope(checkpoint_id: str) -> Iterator[None]:
    """Mark tool calls made by one disposable checkpoint execution."""
    if not isinstance(checkpoint_id, str) or not checkpoint_id:
        raise ValueError("checkpoint execution requires an exact id")
    token = _current_checkpoint_id.set(checkpoint_id)
    try:
        yield
    finally:
        _current_checkpoint_id.reset(token)


def current_checkpoint_id() -> str | None:
    """Return the backend-owned checkpoint marker for this execution."""
    return _current_checkpoint_id.get()


def _checkpoint(
    db: sqlite3.Connection, checkpoint_id: str, thread_id: str
) -> sqlite3.Row:
    row = db.execute(
        "SELECT id,thread_id,history_epoch,status,continuity_base_revision,"
        "continuity_base_snapshot_json FROM checkpoints WHERE id=?",
        (checkpoint_id,),
    ).fetchone()
    if row is None or row["thread_id"] != thread_id or row["status"] != "running":
        raise ContinuityError("checkpoint execution is no longer running")
    return row


def running_checkpoint_for_thread(
    db: sqlite3.Connection, checkpoint_id: str, thread_id: str
) -> bool:
    """Whether authenticated transport metadata names this running checkpoint."""
    row = db.execute(
        "SELECT 1 FROM checkpoints WHERE id=? AND thread_id=? AND status='running'",
        (checkpoint_id, thread_id),
    ).fetchone()
    return row is not None


def staged_edit_count(db: sqlite3.Connection, checkpoint_id: str) -> int:
    """Count edits successfully staged by one checkpoint execution."""
    return int(
        db.execute(
            "SELECT COUNT(*) FROM checkpoint_staged_edits WHERE checkpoint_id=?",
            (checkpoint_id,),
        ).fetchone()[0]
    )


def freeze_continuity(
    db: sqlite3.Connection, thread_id: str, history_epoch: int
) -> tuple[int, str]:
    """Freeze entry bodies and revisions inside checkpoint admission."""
    snapshot = active_entry_snapshot(db, thread_id, history_epoch)
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return store_revision(db, thread_id, history_epoch), encoded


def _rows(db: sqlite3.Connection, checkpoint_id: str) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM checkpoint_staged_edits WHERE checkpoint_id=? "
        "ORDER BY edit_order",
        (checkpoint_id,),
    ).fetchall()


def _base_entries(row: sqlite3.Row) -> dict[int, dict[str, Any]]:
    decoded = json.loads(str(row["continuity_base_snapshot_json"]))
    return {int(entry["entry_id"]): dict(entry) for entry in decoded}


def frozen_continuity_index(
    checkpoint_id: str,
    thread_id: str,
    *,
    db: sqlite3.Connection | None = None,
) -> str:
    """Return only field and entry IDs from the checkpoint's frozen snapshot."""
    if db is None:
        from infra.database import get_db

        connection = get_db()
    else:
        connection = db
    close = db is None
    try:
        checkpoint = _checkpoint(connection, checkpoint_id, thread_id)
        fields: dict[str, list[int]] = {}
        for entry in _base_entries(checkpoint).values():
            fields.setdefault(str(entry["field"]), []).append(int(entry["entry_id"]))
        return json.dumps(
            {field: sorted(ids) for field, ids in sorted(fields.items())},
            sort_keys=True,
            separators=(",", ":"),
        )
    finally:
        if close:
            connection.close()


def _virtual_entries(
    base: dict[int, dict[str, Any]], rows: list[sqlite3.Row]
) -> dict[int, dict[str, Any]]:
    entries = {entry_id: dict(entry) for entry_id, entry in base.items()}
    for row in rows:
        entry_id = int(row["entry_id"])
        if row["operation"] == "add":
            entries[entry_id] = {
                "entry_id": entry_id,
                "field": str(row["field"]),
                "body": json.loads(str(row["body"])),
                "revision": None,
            }
        elif row["operation"] == "replace" and entry_id in entries:
            entries[entry_id]["body"] = json.loads(str(row["body"]))
        elif row["operation"] == "drop":
            entries.pop(entry_id, None)
    return entries


def _grouped(entries: dict[int, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {field: [] for field in ENTRY_FIELDS}
    for entry in sorted(entries.values(), key=lambda item: int(item["entry_id"])):
        grouped[str(entry["field"])].append(
            {"entry_id": int(entry["entry_id"]), **dict(entry["body"])}
        )
    return grouped


def _inspect_result(
    revision: int,
    entries: dict[int, dict[str, Any]],
    entry_id: int | None,
    field: str | None = None,
) -> dict[str, Any]:
    if entry_id is None:
        grouped = _grouped(entries)
        if field is not None:
            grouped = {field: grouped[field]}
        return {"revision": revision, "entries": grouped}
    target = entries.get(entry_id)
    if target is None:
        raise ContinuityError(f"continuity entry {entry_id} does not exist")
    if field is not None:
        check_field_match(field, entry_id, str(target["field"]))
    return {
        "revision": revision,
        "entry": {
            "entry_id": entry_id,
            "field": str(target["field"]),
            **dict(target["body"]),
        },
    }


def _insert(
    db: sqlite3.Connection,
    checkpoint_id: str,
    *,
    operation: str,
    entry_id: int,
    field: str,
    body: str | None = None,
    reason: str | None = None,
    source_ref: str | None = None,
    base_revision: int | None = None,
) -> None:
    order = int(
        db.execute(
            "SELECT COALESCE(MAX(edit_order),0)+1 FROM checkpoint_staged_edits "
            "WHERE checkpoint_id=?",
            (checkpoint_id,),
        ).fetchone()[0]
    )
    db.execute(
        "INSERT INTO checkpoint_staged_edits "
        "(checkpoint_id,edit_order,operation,entry_id,field,body,reason,source_ref,"
        "base_entry_revision) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            checkpoint_id,
            order,
            operation,
            entry_id,
            field,
            body,
            reason,
            source_ref,
            base_revision,
        ),
    )


def stage_edit(
    db: sqlite3.Connection,
    *,
    checkpoint_id: str,
    thread_id: str,
    operation: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Inspect or stage one edit against the checkpoint's frozen view."""
    checkpoint = _checkpoint(db, checkpoint_id, thread_id)
    prior = _rows(db, checkpoint_id)
    entries = _virtual_entries(_base_entries(checkpoint), prior)
    if operation == "inspect":
        entry_id = params.get("entry_id")
        return _inspect_result(
            int(checkpoint["continuity_base_revision"]),
            entries,
            int(entry_id) if entry_id is not None else None,
            params.get("field"),
        )
    if operation == "add":
        return _stage_add(db, checkpoint, entries, params)
    if operation == "review":
        from infra.placement_marks import stage_checkpoint_review

        insert = functools.partial(_insert, db, str(checkpoint["id"]))
        return stage_checkpoint_review(db, thread_id, entries, prior, params, insert=insert)
    entry_id = int(params["entry_id"])
    target = entries.get(entry_id)
    if target is None:
        raise ContinuityError(f"continuity entry {entry_id} does not exist")
    field = str(target["field"])
    supplied_field = params.get("field")
    if supplied_field is not None:
        check_field_match(supplied_field, entry_id, field)
    base_revision = target.get("revision")
    if operation == "replace":
        body = validate_entry_body(field, params["body"])
        _insert(
            db, checkpoint_id, operation=operation, entry_id=entry_id,
            field=field, body=body, base_revision=base_revision,
        )
    else:
        reason, source = validate_retirement(
            field, params["reason"], params.get("source_ref")
        )
        _insert(
            db, checkpoint_id, operation=operation, entry_id=entry_id,
            field=field, reason=reason, source_ref=source,
            base_revision=base_revision,
        )
    return {"operation": operation, "entry_id": entry_id, "field": field}


def _stage_add(
    db: sqlite3.Connection,
    checkpoint: sqlite3.Row,
    entries: dict[int, dict[str, Any]],
    params: dict[str, Any],
) -> dict[str, Any]:
    field = str(params["field"])
    body = validate_entry_body(field, params["body"])
    if sum(entry["field"] == field for entry in entries.values()) >= MAX_LIST_ITEMS:
        raise ContinuityError(f"{field} already holds the maximum active entries")
    entry_id = reserve_entry_id(
        db, str(checkpoint["thread_id"]), int(checkpoint["history_epoch"])
    )
    _insert(
        db, str(checkpoint["id"]), operation="add", entry_id=entry_id,
        field=field, body=body,
    )
    return {"operation": "add", "entry_id": entry_id, "field": field}


def _mark(
    db: sqlite3.Connection,
    checkpoint_id: str,
    edit_order: int,
    outcome: str,
    detail: str | None = None,
) -> None:
    db.execute(
        "UPDATE checkpoint_staged_edits SET outcome=?,outcome_detail=?,"
        "resolved_at=CURRENT_TIMESTAMP WHERE checkpoint_id=? AND edit_order=?",
        (outcome, detail, checkpoint_id, edit_order),
    )


def merge_staged_edits(db: sqlite3.Connection, checkpoint_id: str) -> str:
    """Merge non-conflicting edits and return the exact resolved snapshot."""
    checkpoint = db.execute(
        "SELECT * FROM checkpoints WHERE id=?", (checkpoint_id,)
    ).fetchone()
    if checkpoint is None:
        raise ContinuityError(f"checkpoint {checkpoint_id} does not exist")
    thread_id = str(checkpoint["thread_id"])
    epoch = int(checkpoint["history_epoch"])
    base = _base_entries(checkpoint)
    current = {entry["entry_id"]: entry for entry in active_entry_snapshot(
        db, thread_id, epoch
    )}
    conflicts = {
        entry_id for entry_id, entry in base.items()
        if entry_id not in current
        or int(current[entry_id]["revision"]) != int(entry["revision"])
    }
    for row in _rows(db, checkpoint_id):
        entry_id = int(row["entry_id"])
        if entry_id in conflicts:
            _mark(db, checkpoint_id, int(row["edit_order"]), "conflict", "live entry changed")
            continue
        try:
            _apply_row(db, checkpoint, row)
        except ContinuityConflict as exc:
            conflicts.add(entry_id)
            _mark(db, checkpoint_id, int(row["edit_order"]), "conflict", str(exc))
        else:
            _mark(db, checkpoint_id, int(row["edit_order"]), "applied")
    resolved = active_entry_snapshot(db, thread_id, epoch)
    return json.dumps(resolved, sort_keys=True, separators=(",", ":"))


def _apply_row(
    db: sqlite3.Connection, checkpoint: sqlite3.Row, row: sqlite3.Row
) -> None:
    common = {
        "thread_id": str(checkpoint["thread_id"]),
        "history_epoch": int(checkpoint["history_epoch"]),
    }
    operation = str(row["operation"])
    entry_id = int(row["entry_id"])
    if operation == "add":
        add_reserved_entry(
            db, **common, entry_id=entry_id, field=str(row["field"]),
            body=json.loads(str(row["body"])),
        )
    elif operation == "replace":
        replace_entry(
            db, **common, entry_id=entry_id, body=json.loads(str(row["body"]))
        )
    elif operation == "review":
        from infra.placement_marks import apply_review

        apply_review(db, str(checkpoint["id"]), row, common)
    else:
        retire_entry(
            db, **common, entry_id=entry_id, reason=str(row["reason"]),
            source_ref=row["source_ref"],
        )
