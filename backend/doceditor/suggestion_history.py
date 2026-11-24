"""Storage for non-live suggestion decision history.

The decision writer owns the surrounding transaction: a history entry and its decisions
must either both be recorded or neither be recorded. Which suggestions an event decided, and
whether that decision stands, is read from doc_comments (decision_event_id and decision).
"""

import json
import sqlite3
from typing import Any


def record_history(
    db: sqlite3.Connection, event_id: str, artifact_id: str, rows: list[dict[str, Any]], before: str, after: str,
) -> None:
    fields = ('decided_by', 'decided_by_thread_id', 'decided_at', 'decided_in_bulk')
    metadata = {row['id']: {field: row.get(field) for field in fields} for row in rows}
    db.execute(
        "INSERT INTO doc_suggestion_history "
        "(event_id, artifact_id, decision_metadata, before_sha256, after_sha256) "
        "VALUES (?, ?, ?, ?, ?)",
        (event_id, artifact_id, json.dumps(metadata), before, after),
    )
