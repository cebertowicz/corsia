"""Persist review-fork identity and the request-owned lifecycle."""

from __future__ import annotations

import re
import sqlite3


def _remove_cancelled_at(conn: sqlite3.Connection) -> None:
    """Rebuild the legacy table without its duplicate state column, CHECK and batch link.

    Preserve the installed DDL and its indexes/triggers, including nullable
    legacy fixture columns. The migration runner holds foreign_keys=OFF.
    """
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='doc_review_requests'",
    ).fetchone()[0]
    sql = re.sub(r"\bcancelled_at\s+TEXT\s*,\s*", "", sql, flags=re.IGNORECASE)
    sql = re.sub(
        r",\s*CHECK\s*\(\s*cancelled_at\s+IS\s+NULL\s+OR\s+answered_batch_id\s+IS\s+NULL\s*\)",
        "", sql, flags=re.IGNORECASE,
    )
    link = r"answered_batch_id\s+TEXT(?:\s+REFERENCES\s+\w+\s*\(\s*\w+\s*\))?"
    sql = re.sub(rf"\b{link}\s*,\s*|,\s*{link}(?=\s*\))", "", sql, count=1, flags=re.IGNORECASE)
    if re.search(r"\b(cancelled_at|answered_batch_id)\b", sql, flags=re.IGNORECASE):
        raise ValueError("review request cancellation schema could not be retired")
    sql = re.sub(r"\bdoc_review_requests\b", "doc_review_requests_new", sql, count=1)
    objects = conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name='doc_review_requests' "
        "AND type IN ('index','trigger') AND sql IS NOT NULL",
    ).fetchall()
    columns = [str(row[1]) for row in conn.execute("PRAGMA table_info(doc_review_requests)")
               if row[1] not in {"cancelled_at", "answered_batch_id"}]
    names = ",".join('"' + name.replace('"', '""') + '"' for name in columns)
    conn.execute(sql)
    conn.execute(f"INSERT INTO doc_review_requests_new ({names}) SELECT {names} FROM doc_review_requests")
    conn.execute("DROP TABLE doc_review_requests")
    conn.execute("ALTER TABLE doc_review_requests_new RENAME TO doc_review_requests")
    for (statement,) in objects:
        conn.execute(statement)


def migrate_132_to_133(conn: sqlite3.Connection) -> None:
    """Backfill the lifecycle from the legacy facts, then remove them."""
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
    if "cancelled_at" in columns:
        conn.execute(
            "UPDATE doc_review_requests SET state=CASE WHEN cancelled_at IS NOT NULL THEN 'withdrawn' "
            "WHEN answered_batch_id IS NOT NULL THEN 'submitted' ELSE 'handed_back' END, "
            "state_changed_at=COALESCE(cancelled_at,(SELECT b.updated_at FROM doc_feedback_batches b "
            "WHERE b.id=answered_batch_id),created_at) WHERE state_changed_at IS NULL"
        )
        _remove_cancelled_at(conn)
    else:
        conn.execute("UPDATE doc_review_requests SET state_changed_at=created_at WHERE state_changed_at IS NULL")
