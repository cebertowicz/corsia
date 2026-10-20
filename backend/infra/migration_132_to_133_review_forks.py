"""Persist review-fork identity and the request-owned lifecycle."""

from __future__ import annotations

import sqlite3


def migrate_132_to_133(conn: sqlite3.Connection) -> None:
    """Existing node reviews start handed back; new requests explicitly start forked."""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(doc_review_requests)")}
    if not columns:
        return
    additions = {
        "fork_id": "TEXT",
        "state": "TEXT NOT NULL DEFAULT 'handed_back' CHECK (state IN ('forked','submitted','withdrawn','handed_back','failed'))",
        "state_changed_at": "TEXT",
        "reason": "TEXT",
    }
    for name, definition in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE doc_review_requests ADD COLUMN {name} {definition}")
    conn.execute(
        "UPDATE doc_review_requests SET state=CASE WHEN cancelled_at IS NOT NULL THEN 'withdrawn' "
        "WHEN answered_batch_id IS NOT NULL THEN 'submitted' "
        "WHEN fork_id IS NOT NULL AND node_id IS NULL THEN 'forked' ELSE 'handed_back' END, "
        "state_changed_at=COALESCE(cancelled_at,created_at) WHERE state_changed_at IS NULL"
    )
