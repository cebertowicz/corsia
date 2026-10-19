"""Schema 127 -> 128: declared measure definitions, per-agent per-day intake counters, and a
script schedule's last measure summary.

The counters are keyed agent, day, event, counter so one agent's recent window is a single
index range; ``idx_outcome_events_agent_observed`` serves the same read over its rows.
``idx_measure_definitions_kind_name`` serves sync's one in-transaction read, a namespace's
event rows.

The DDL is copied rather than imported from ``infra.outcome_events_schema`` so this step
keeps producing the shape it was written for. The registry tables are created only when
the startup schema pass has not already made them, and the counter table is rebuilt only
from its old shape, so the step is safe to run after that pass.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3

_COUNTERS_128_DDL = """
    CREATE TABLE outcome_event_counters (
        counter TEXT NOT NULL CHECK (counter IN (
            'duplicate_ignored', 'attribute_dropped', 'mapping_failed',
            'name_undeclared', 'namespace_not_owned',
            'record_refused', 'unversioned')),
        event_name TEXT NOT NULL,
        agent_thread_id TEXT NOT NULL DEFAULT '',
        day TEXT NOT NULL DEFAULT '0000-00-00',
        n INTEGER NOT NULL DEFAULT 0 CHECK (n >= 0),
        PRIMARY KEY (agent_thread_id, day, event_name, counter)
    )
"""

_REGISTRY_128_DDL = (
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
        removed INTEGER NOT NULL DEFAULT 0 CHECK (removed IN (0, 1)),
        PRIMARY KEY (owner_thread_id, kind, name, version)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_measure_definitions_kind_name
        ON measure_definitions(kind, name)
    """,
)

_AGENT_OBSERVED_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS idx_outcome_events_agent_observed
        ON outcome_events(agent_thread_id, observed_ts, event_name)
        WHERE agent_thread_id IS NOT NULL
"""


def migrate_127_to_128(conn: sqlite3.Connection) -> None:
    """Create the registry and index, and widen counters without losing existing totals."""
    for statement in _REGISTRY_128_DDL:
        conn.execute(statement)
    # Migration 77 creates outcome_events; the index is made only where that table exists.
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='outcome_events'"
    ).fetchone():
        conn.execute(_AGENT_OBSERVED_INDEX_DDL)
    schedule_columns = {row[1] for row in conn.execute("PRAGMA table_info(automation_schedules)")}
    if schedule_columns and "last_measures" not in schedule_columns:
        conn.execute("ALTER TABLE automation_schedules ADD COLUMN last_measures TEXT")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(outcome_event_counters)")}
    if "agent_thread_id" in columns:
        return
    if not columns:
        conn.execute(_COUNTERS_128_DDL)
        return
    conn.execute("ALTER TABLE outcome_event_counters RENAME TO outcome_event_counters_old")
    conn.execute(_COUNTERS_128_DDL)
    conn.execute(
        "INSERT INTO outcome_event_counters(counter, event_name, agent_thread_id, day, n) "
        "SELECT counter, event_name, '', '0000-00-00', n FROM outcome_event_counters_old"
    )
    conn.execute("DROP TABLE outcome_event_counters_old")
