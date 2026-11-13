"""Schema 192 -> 193: allow event rules for chats outside projects."""
from __future__ import annotations

import sqlite3

from infra.migration_170_to_171_worker_profile import _RULES as _RULES_171

_OLD_SCOPE = "scope TEXT NOT NULL CHECK (scope LIKE 'project:_%'"
_RULES = _RULES_171.replace(
    _OLD_SCOPE, _OLD_SCOPE + " OR scope = 'workspace'", 1)
assert _RULES != _RULES_171


def migrate_192_to_193(conn: sqlite3.Connection) -> None:
    """Rebuild the table, preserving every row and its indexes and triggers."""
    objects = [row[0] for row in conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name='event_rules' "
        "AND type IN ('index', 'trigger') AND sql IS NOT NULL ORDER BY type, name")]
    conn.execute("ALTER TABLE event_rules RENAME TO event_rules_192")
    conn.execute(_RULES)
    names = ",".join(f'"{row[1]}"' for row in conn.execute("PRAGMA table_info(event_rules)"))
    conn.execute(f"INSERT INTO event_rules ({names}) SELECT {names} FROM event_rules_192")
    conn.execute("DROP TABLE event_rules_192")
    for statement in objects:
        conn.execute(statement)
