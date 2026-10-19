"""Schema 126 -> 127: declared measure definitions and per-agent, per-day intake counters.

The DDL is copied rather than imported from ``infra.outcome_events_schema`` so this step
keeps producing the shape it was written for. The registry tables are created only when
the startup schema pass has not already made them, and the counter table is rebuilt only
from its old shape, so the step is safe to run after that pass.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3

_COUNTERS_127_DDL = """
    CREATE TABLE outcome_event_counters (
        counter TEXT NOT NULL CHECK (counter IN (
            'duplicate_ignored', 'attribute_dropped', 'mapping_failed',
            'name_undeclared', 'namespace_not_owned', 'identity_mismatch',
            'rate_limited', 'record_refused', 'unversioned')),
        event_name TEXT NOT NULL,
        agent_thread_id TEXT NOT NULL DEFAULT '',
        day TEXT NOT NULL DEFAULT '0000-00-00',
        n INTEGER NOT NULL DEFAULT 0 CHECK (n >= 0),
        PRIMARY KEY (counter, event_name, agent_thread_id, day)
    )
"""

_REGISTRY_127_DDL = (
    """
    CREATE TABLE IF NOT EXISTS measure_definitions (
        owner_thread_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('event', 'kpi', 'view')),
        name TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version > 0),
        spec_json TEXT NOT NULL CHECK (json_valid(spec_json)),
        file_path TEXT NOT NULL,
        file_sha256 TEXT NOT NULL,
        synced_at TEXT NOT NULL,
        PRIMARY KEY (owner_thread_id, kind, name, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS measure_namespaces (
        namespace TEXT PRIMARY KEY,
        owner_thread_id TEXT NOT NULL
    )
    """,
)


def migrate_126_to_127(conn: sqlite3.Connection) -> None:
    """Create the registry and widen counters without losing existing totals."""
    for statement in _REGISTRY_127_DDL:
        conn.execute(statement)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(outcome_event_counters)")}
    if "agent_thread_id" in columns:
        return
    if not columns:
        conn.execute(_COUNTERS_127_DDL)
        return
    conn.execute("ALTER TABLE outcome_event_counters RENAME TO outcome_event_counters_old")
    conn.execute(_COUNTERS_127_DDL)
    conn.execute(
        "INSERT INTO outcome_event_counters(counter, event_name, agent_thread_id, day, n) "
        "SELECT counter, event_name, '', '0000-00-00', n FROM outcome_event_counters_old"
    )
    conn.execute("DROP TABLE outcome_event_counters_old")
