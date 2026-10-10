"""Schema 85 -> 86: document feedback items can identify comment events."""
from __future__ import annotations

import sqlite3


def migrate_85_to_86(conn: sqlite3.Connection) -> None:
    """Add optional event metadata without rewriting existing feedback items."""
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(doc_feedback_batch_items)")
    }
    additions = (
        ("event", "TEXT CHECK (event IS NULL OR event='reopened')"),
        ("event_at", "TEXT"),
        ("event_by", "TEXT"),
    )
    for name, definition in additions:
        if name not in columns:
            conn.execute(f"ALTER TABLE doc_feedback_batch_items ADD COLUMN {name} {definition}")
