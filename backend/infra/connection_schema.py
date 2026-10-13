"""Provider transport and ordered connector event rules."""
from __future__ import annotations

CONNECTION_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS connections (
        id TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        provider TEXT NOT NULL,
        config TEXT,
        status TEXT NOT NULL DEFAULT 'available',
        enabled INTEGER NOT NULL DEFAULT 1,
        scope TEXT NOT NULL DEFAULT 'global',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(provider)
    );
    CREATE INDEX IF NOT EXISTS idx_connections_status ON connections(type, status);

    -- Connections rule binder (coordination MVP S3). Ordered rules bind one declared
    -- Event-Model filterable fact + value to a registered destination thread; the
    -- binder owns first-seen owner assignment, then existing artifacts route by
    -- ownership (not by re-running rules). Precedence is explicit; equal-precedence
    -- ties surface a binder-repair artifact, never a silent tiebreak.
    CREATE TABLE IF NOT EXISTS connection_rules (
        id TEXT PRIMARY KEY,
        scope TEXT NOT NULL DEFAULT 'global',
        connector TEXT NOT NULL,
        fact TEXT NOT NULL,
        value TEXT NOT NULL,
        destination_thread_id TEXT NOT NULL,
        precedence INTEGER NOT NULL DEFAULT 100,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_connection_rules_match
        ON connection_rules(connector, enabled, precedence, created_at);
"""
