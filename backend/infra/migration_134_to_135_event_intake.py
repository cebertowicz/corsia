"""Schema 134 -> 135: receipt rows for declared-event intake deduplication."""
from __future__ import annotations

import sqlite3


def migrate_134_to_135(conn: sqlite3.Connection) -> None:
    """Create the receipt table used to return duplicate IntakeResults."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS event_intake_records ("
        "event_id TEXT PRIMARY KEY, outcome TEXT NOT NULL CHECK(outcome='accepted'), "
        "rules_matched INTEGER NOT NULL)"
    )
