"""Schema 76 -> 77: the outcome event table and its intake counters.

Purely additive: new tables and indexes, no data to convert. The DDL is copied
rather than imported from ``infra.outcome_events_schema`` so this step keeps
producing the shape it was written for even after that module's DDL changes.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3

_OUTCOME_EVENTS_77_DDL = (
    """
    CREATE TABLE IF NOT EXISTS outcome_events (
        id              TEXT PRIMARY KEY,
        dedupe_key      TEXT NOT NULL UNIQUE CHECK (length(trim(dedupe_key)) > 0),
        event_name      TEXT NOT NULL CHECK (length(trim(event_name)) > 0),
        source          TEXT NOT NULL
                          CHECK (source IN ('github', 'corsia', 'agent', 'otlp')),
        provenance      TEXT NOT NULL
                          CHECK (provenance IN (
                            'observed', 'imported', 'instrumented', 'agent_recorded')),
        ts              TEXT NOT NULL,
        observed_ts     TEXT NOT NULL,
        subject_key     TEXT,
        project_id      TEXT,
        agent_thread_id TEXT,
        attribution     TEXT CHECK (attribution IS NULL OR attribution IN (
                          'title_prefix', 'body_owner', 'branch', 'caller', 'none')),
        corroborated_by TEXT,
        attributes      TEXT NOT NULL
                          CHECK (json_valid(attributes)
                                 AND json_type(attributes) = 'object'),
        retracted_at    TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_outcome_events_name_ts"
    " ON outcome_events(event_name, ts)",
    "CREATE INDEX IF NOT EXISTS idx_outcome_events_agent_ts"
    " ON outcome_events(agent_thread_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_outcome_events_project_ts"
    " ON outcome_events(project_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_outcome_events_subject"
    " ON outcome_events(subject_key)",
    """
    CREATE TABLE IF NOT EXISTS outcome_event_counters (
        counter    TEXT NOT NULL CHECK (counter IN (
                     'duplicate_ignored', 'attribute_dropped', 'mapping_failed')),
        event_name TEXT NOT NULL,
        n          INTEGER NOT NULL DEFAULT 0 CHECK (n >= 0),
        PRIMARY KEY (counter, event_name)
    )
    """,
)


def migrate_76_to_77(conn: sqlite3.Connection) -> None:
    """Create the outcome event tables on an existing database."""
    for statement in _OUTCOME_EVENTS_77_DDL:
        conn.execute(statement)
