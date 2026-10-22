"""Rules that run scripts: ``event_rules.script_id`` and the run outcomes of ``event_route_decisions``.

SQLite can't widen a CHECK in place, so both tables are rebuilt from the
fresh-install DDL and their rows copied over. Guarded: a table that already has
its new column is left alone.
"""
from __future__ import annotations

import sqlite3

from infra.event_rule_schema import EVENT_RULE_SCHEMA_SQL

_NEW_COLUMNS = {"event_rules": "script_id", "event_route_decisions": "run_input"}


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _create(conn: sqlite3.Connection) -> None:
    # Statement by statement so the runner's migration transaction stays intact.
    for statement in EVENT_RULE_SCHEMA_SQL.split(";"):
        if statement.strip():
            conn.execute(statement)


def migrate_138_to_139(conn: sqlite3.Connection) -> None:
    rebuilt = []
    for table, column in _NEW_COLUMNS.items():
        present = _columns(conn, table)
        if present and column not in present:
            conn.execute(f"ALTER TABLE {table} RENAME TO {table}_138")
            rebuilt.append(table)
    _create(conn)
    for table in rebuilt:
        kept = set(_columns(conn, table))
        shared = ",".join(f'"{name}"' for name in _columns(conn, f"{table}_138") if name in kept)
        conn.execute(f"INSERT INTO {table} ({shared}) SELECT {shared} FROM {table}_138")
        conn.execute(f"DROP TABLE {table}_138")
    _create(conn)  # the renamed tables kept the index names until they were dropped
