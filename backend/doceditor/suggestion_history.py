"""Storage for non-live suggestion decision history.

The decision writer owns the surrounding transaction: a history entry and its decisions
must either both be recorded or neither be recorded.
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
        "(event_id, artifact_id, suggestion_ids, decision_metadata, before_sha256, after_sha256, state) "
        "VALUES (?, ?, ?, ?, ?, ?, 'accepted')",
        (event_id, artifact_id, json.dumps([row['id'] for row in rows]), json.dumps(metadata), before, after),
    )
