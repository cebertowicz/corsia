"""Schema migration for declared Agent KPI measures and per-agent counters."""

from __future__ import annotations

import sqlite3


def migrate_126_to_127(conn: sqlite3.Connection) -> None:
    """Create the registry and widen counters without losing existing totals."""
    conn.executescript(
        """
        CREATE TABLE measure_definitions (
            owner_thread_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('event', 'kpi', 'view')),
            name TEXT NOT NULL,
            version INTEGER NOT NULL CHECK (version > 0),
            spec_json TEXT NOT NULL CHECK (json_valid(spec_json)),
            file_path TEXT NOT NULL,
            file_sha256 TEXT NOT NULL,
            synced_at TEXT NOT NULL,
            PRIMARY KEY (owner_thread_id, kind, name, version)
        );
        CREATE TABLE measure_namespaces (
            namespace TEXT PRIMARY KEY,
            owner_thread_id TEXT NOT NULL
        );
        ALTER TABLE outcome_event_counters RENAME TO outcome_event_counters_old;
        CREATE TABLE outcome_event_counters (
            counter TEXT NOT NULL CHECK (counter IN (
                'duplicate_ignored', 'attribute_dropped', 'mapping_failed',
                'name_undeclared', 'namespace_not_owned', 'identity_mismatch',
                'rate_limited', 'record_refused')),
            event_name TEXT NOT NULL,
            agent_thread_id TEXT NOT NULL DEFAULT '',
            day TEXT NOT NULL DEFAULT '0000-00-00',
            n INTEGER NOT NULL DEFAULT 0 CHECK (n >= 0),
            PRIMARY KEY (counter, event_name, agent_thread_id, day)
        );
        INSERT INTO outcome_event_counters(counter, event_name, agent_thread_id, day, n)
            SELECT counter, event_name, '', '0000-00-00', n
            FROM outcome_event_counters_old;
        DROP TABLE outcome_event_counters_old;
        """
    )
