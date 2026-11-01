"""Schema 168: measure keys for the OTLP receiver, and the ``identity_mismatch`` intake counter.

Frozen: it writes the two tables directly and imports no intake code, which moves on.
"""
import sqlite3

_KEYS_DDL = """
    CREATE TABLE IF NOT EXISTS measure_keys (
        key_id          TEXT PRIMARY KEY,
        key_hash        TEXT NOT NULL UNIQUE,
        user_id         TEXT NOT NULL,
        agent_thread_id TEXT NOT NULL,
        label           TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        revoked_at      TEXT
    )
"""
_COUNTERS_168_DDL = """
    CREATE TABLE outcome_event_counters (
        counter TEXT NOT NULL CHECK (counter IN (
            'duplicate_ignored', 'attribute_dropped', 'mapping_failed',
            'name_undeclared', 'namespace_not_owned', 'identity_mismatch',
            'record_refused', 'unversioned')),
        event_name TEXT NOT NULL,
        agent_thread_id TEXT NOT NULL DEFAULT '',
        day TEXT NOT NULL DEFAULT '0000-00-00',
        n INTEGER NOT NULL DEFAULT 0 CHECK (n >= 0),
        PRIMARY KEY (agent_thread_id, day, event_name, counter)
    )
"""


def migrate_167_to_168(conn: sqlite3.Connection) -> None:
    conn.execute(_KEYS_DDL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_measure_keys_agent ON measure_keys(agent_thread_id, created_at)")
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outcome_event_counters'").fetchone():
        conn.execute(_COUNTERS_168_DDL)
        return
    conn.execute("ALTER TABLE outcome_event_counters RENAME TO outcome_event_counters_old")
    conn.execute(_COUNTERS_168_DDL)
    conn.execute(
        "INSERT INTO outcome_event_counters(counter, event_name, agent_thread_id, day, n) "
        "SELECT counter, event_name, agent_thread_id, day, n FROM outcome_event_counters_old"
    )
    conn.execute("DROP TABLE outcome_event_counters_old")
