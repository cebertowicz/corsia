"""Schema 157 -> 158: separate self-caused events and shell-only prefixes."""
from __future__ import annotations

import sqlite3

from infra.event_rule_schema import EVENT_RULE_SCHEMA_SQL
from infra.migration_152_to_153_observe_rules import _create


def migrate_157_to_158(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE event_rules RENAME TO event_rules_157")
    _create(conn, EVENT_RULE_SCHEMA_SQL)
    # Every 157 observe row is a shell rule, so rows copy unchanged under the relaxed CHECK.
    columns = [row[1] for row in conn.execute("PRAGMA table_info(event_rules)")]
    names = ",".join(f'"{name}"' for name in columns)
    conn.execute(f"INSERT INTO event_rules ({names}) SELECT {names} FROM event_rules_157")
    conn.execute("DROP TABLE event_rules_157")
    _create(conn, EVENT_RULE_SCHEMA_SQL)
