"""Read-only predicates over queued thread-input rows."""

from __future__ import annotations

import json
import sqlite3


def starts_turn(row: sqlite3.Row) -> bool:
    """Return false only for a passive context event waiting for activity."""
    if row["role"] != "system":
        return True
    metadata = json.loads(row["metadata"] or "{}")
    return not (
        isinstance(metadata, dict)
        and metadata.get("type") == "context_event"
        and metadata.get("event_disposition") == "passive"
    )
