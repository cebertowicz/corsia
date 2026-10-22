"""Schema 136 -> 137: receipt rows for declared-event intake deduplication."""
from __future__ import annotations

import sqlite3

from infra.event_intake_schema import EVENT_INTAKE_SCHEMA_SQL


def migrate_136_to_137(conn: sqlite3.Connection) -> None:
    """Create the receipt table used to return duplicate IntakeResults."""
    # Statement by statement so the runner's migration transaction stays intact.
    for statement in EVENT_INTAKE_SCHEMA_SQL.split(";"):
        if statement.strip():
            conn.execute(statement)
