"""Schema 170 -> 171: a ``start_worker`` rule may name the workflow profile its worker runs under.

``event_rules.worker_profile`` is nullable text; a rule written before this has none, which reads
as the workflow's base settings. The table's CHECKs sit after its columns, so ``ALTER TABLE ADD
COLUMN`` would leave text that differs from a fresh install's: the table is rebuilt from its own
fixed text, 161's table plus the column, so a later schema edit never moves what this step
creates (as 160 -> 161 keeps its own copy), keeping every row. Idempotent: a table that has the
column is left alone.
"""

from __future__ import annotations

import sqlite3

from infra.migration_160_to_161_self_caused import _RULES as _RULES_161

_ITEM_TYPE = "        item_type TEXT,\n"
_RULES = _RULES_161.replace(_ITEM_TYPE, _ITEM_TYPE + (
    "        -- start_worker: the workflow profile its worker runs under (none: the workflow's base settings).\n"
    "        worker_profile TEXT CHECK (worker_profile IS NULL OR action = 'start_worker'),\n"), 1)
assert _RULES != _RULES_161, "161's event_rules text no longer has the item_type line"

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_event_rules_type\n        ON event_rules(connector, event_type, enabled, scope, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_event_rules_scope\n        ON event_rules(scope, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_event_rules_observe\n        ON event_rules(tool, enabled, scope) WHERE kind = 'observe'",
)


def migrate_170_to_171(conn: sqlite3.Connection) -> None:
    """Rebuild ``event_rules`` with the nullable ``worker_profile`` column."""
    if "worker_profile" in {str(row[1]) for row in conn.execute("PRAGMA table_info(event_rules)")}:
        return
    conn.execute("ALTER TABLE event_rules RENAME TO event_rules_170")
    conn.execute(_RULES)
    old = {str(row[1]) for row in conn.execute("PRAGMA table_info(event_rules_170)")}
    names = ",".join(f'"{row[1]}"' for row in conn.execute("PRAGMA table_info(event_rules)")
                     if row[1] in old)
    conn.execute(f"INSERT INTO event_rules ({names}) SELECT {names} FROM event_rules_170")
    conn.execute("DROP TABLE event_rules_170")
    for statement in _INDEXES:
        conn.execute(statement)
