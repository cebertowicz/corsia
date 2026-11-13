"""Schema 191 -> 192: allow event rules for chats outside projects."""
from __future__ import annotations

import sqlite3

from infra.delegation_scope import WORKSPACE_SCOPE_ID
from infra.migration_170_to_171_worker_profile import _RULES as _RULES_171

_OLD_SCOPE = "scope TEXT NOT NULL CHECK (scope LIKE 'project:_%'"
_RULES = _RULES_171.replace(
    _OLD_SCOPE, _OLD_SCOPE + f" OR scope = '{WORKSPACE_SCOPE_ID}'", 1)
assert _RULES != _RULES_171


def migrate_191_to_192(conn: sqlite3.Connection) -> None:
    """Rebuild the table, preserving every row and its indexes and triggers."""
    objects = [row[0] for row in conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name='event_rules' "
        "AND type IN ('index', 'trigger') AND sql IS NOT NULL ORDER BY type, name")]
    conn.execute("ALTER TABLE event_rules RENAME TO event_rules_191")
    conn.execute(_RULES)
    names = ",".join(f'"{row[1]}"' for row in conn.execute("PRAGMA table_info(event_rules)"))
    conn.execute(f"INSERT INTO event_rules ({names}) SELECT {names} FROM event_rules_191")
    conn.execute("DROP TABLE event_rules_191")
    for statement in objects:
        conn.execute(statement)
