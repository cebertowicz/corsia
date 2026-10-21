"""Provider transport and ordered connector event rules."""
from __future__ import annotations

from infra.event_rule_schema import EVENT_RULE_SCHEMA_SQL

CONNECTION_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS connections (
        id TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        provider TEXT NOT NULL,
        config TEXT,
        status TEXT NOT NULL DEFAULT 'available',
        scope TEXT NOT NULL DEFAULT 'global',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(provider)
    );
    CREATE INDEX IF NOT EXISTS idx_connections_status ON connections(type, status);

""" + EVENT_RULE_SCHEMA_SQL + """
    CREATE TABLE IF NOT EXISTS event_intake_records (
        event_id TEXT PRIMARY KEY,
        outcome TEXT NOT NULL CHECK(outcome='accepted'),
        rules_matched INTEGER NOT NULL
    );
"""
