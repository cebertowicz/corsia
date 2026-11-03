"""Schema 170 -> 171: a ``start_worker`` rule may name the workflow profile its worker runs under.

``event_rules.worker_profile`` is nullable text; a rule written before this has none, which reads
as the workflow's base settings. The table's CHECKs sit after its columns, so ``ALTER TABLE ADD
COLUMN`` would leave text that differs from a fresh install's: the table is rebuilt from the
current schema text (as 160 -> 161 did), keeping every row. Idempotent: a table that has the
column is left alone.
"""

from __future__ import annotations

import sqlite3

from infra.event_rule_schema import EVENT_RULE_SCHEMA_SQL

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_event_rules_type\n        ON event_rules(connector, event_type, enabled, scope, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_event_rules_scope\n        ON event_rules(scope, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_event_rules_observe\n        ON event_rules(tool, enabled, scope) WHERE kind = 'observe'",
)


def migrate_170_to_171(conn: sqlite3.Connection) -> None:
    """Rebuild ``event_rules`` with the nullable ``worker_profile`` column."""
    if "worker_profile" in {str(row[1]) for row in conn.execute("PRAGMA table_info(event_rules)")}:
        return
    table = next(part for part in EVENT_RULE_SCHEMA_SQL.split(";")
                 if "CREATE TABLE IF NOT EXISTS event_rules (" in part)
    conn.execute("ALTER TABLE event_rules RENAME TO event_rules_170")
    conn.execute(table)
    old = {str(row[1]) for row in conn.execute("PRAGMA table_info(event_rules_170)")}
    names = ",".join(f'"{row[1]}"' for row in conn.execute("PRAGMA table_info(event_rules)")
                     if row[1] in old)
    conn.execute(f"INSERT INTO event_rules ({names}) SELECT {names} FROM event_rules_170")
    conn.execute("DROP TABLE event_rules_170")
    for statement in _INDEXES:
        conn.execute(statement)
