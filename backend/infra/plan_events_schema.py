"""Append-only whole-plan audit schema."""

from __future__ import annotations


PLAN_EVENTS_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS plan_events (
        id TEXT PRIMARY KEY,
        plan_id TEXT NOT NULL REFERENCES plans(id),
        actor_id TEXT,
        provenance TEXT NOT NULL CHECK (length(trim(provenance)) > 0),
        event TEXT NOT NULL CHECK (length(trim(event)) > 0),
        prior_state TEXT,
        new_state TEXT,
        payload_json TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_plan_events_plan
        ON plan_events(plan_id, created_at);
    CREATE TRIGGER IF NOT EXISTS prevent_plan_event_update
    BEFORE UPDATE ON plan_events
    BEGIN
        SELECT RAISE(ABORT, 'plan events are append-only');
    END;
    CREATE TRIGGER IF NOT EXISTS prevent_plan_event_delete
    BEFORE DELETE ON plan_events
    BEGIN
        SELECT RAISE(ABORT, 'plan events are append-only');
    END;
"""
