"""Receipts for declared-event intake: one accepted row per event id, and retained refusals."""
from __future__ import annotations

EVENT_INTAKE_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS event_intake_records (
        id INTEGER PRIMARY KEY,
        event_id TEXT NOT NULL,
        outcome TEXT NOT NULL CHECK(outcome IN ('accepted','refused')),
        rules_matched INTEGER NOT NULL,
        refusal_code TEXT,
        producer TEXT,
        origin TEXT,
        event_type TEXT,
        at TEXT NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_event_intake_accepted
        ON event_intake_records(event_id) WHERE outcome='accepted';
"""
