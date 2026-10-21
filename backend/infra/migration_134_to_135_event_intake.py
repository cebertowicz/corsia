"""Schema 134 -> 135: receipt rows for declared-event intake deduplication."""
from __future__ import annotations

import sqlite3


def migrate_134_to_135(conn: sqlite3.Connection) -> None:
    """Create the receipt table used to return duplicate IntakeResults."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS event_intake_records ("
        "id INTEGER PRIMARY KEY, event_id TEXT NOT NULL, "
        "outcome TEXT NOT NULL CHECK(outcome IN ('accepted','refused')), "
        "rules_matched INTEGER NOT NULL, refusal_code TEXT, producer TEXT, "
        "origin TEXT, event_type TEXT, at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_event_intake_accepted "
        "ON event_intake_records(event_id) WHERE outcome='accepted'"
    )
