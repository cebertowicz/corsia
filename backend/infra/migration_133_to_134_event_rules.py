"""Replace first-seen binding storage with live deliver/drop event rules."""
from __future__ import annotations

import json

from infra.event_rule_schema import EVENT_RULE_SCHEMA_SQL


def migrate_133_to_134(conn) -> None:
    # One DDL source for both existing-table and missing-table upgrades, also
    # used by fresh installs. Statements, not executescript: keep the caller's
    # migration transaction intact.
    for statement in EVENT_RULE_SCHEMA_SQL.split(';'):
        if statement.strip():
            conn.execute(statement)
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='connection_rules'",
    ).fetchone()
    if exists:
        for row in conn.execute('SELECT * FROM connection_rules').fetchall():
            conn.execute(
                'INSERT INTO event_rules '
                '(id,created_at,updated_at,enabled,scope,connector,event_type,"match","on",action,"to",precedence) '
                "VALUES (?,?,?,?,?,?,'*',?,'first_seen','deliver',?,?)",
                (row['id'], row['created_at'], row['updated_at'], row['enabled'],
                 row['scope'], row['connector'],
                 json.dumps([{'field': row['fact'], 'op': 'eq', 'value': row['value']}]),
                 'thread:' + row['destination_thread_id'], row['precedence']),
            )
        conn.execute('DROP TABLE connection_rules')
