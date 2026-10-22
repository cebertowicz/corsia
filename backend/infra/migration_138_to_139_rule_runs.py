"""Rules that run scripts: ``event_rules.script_id``, the run outcomes of ``event_route_decisions``,
and a nullable ``automation_schedules.next_run_at`` for a script only event rules start.

SQLite can't widen a CHECK or drop a NOT NULL in place, so the three tables are
rebuilt from the fresh-install DDL and their rows copied over. Guarded: a table
already in its new shape is left alone.
"""
from __future__ import annotations

import sqlite3

from infra.continuation_schema import AUTOMATION_SCHEDULES_SQL
from infra.event_rule_schema import EVENT_RULE_SCHEMA_SQL

_NEW_COLUMNS = {"event_rules": "script_id", "event_route_decisions": "run_input"}


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _create(conn: sqlite3.Connection, sql: str) -> None:
    # Statement by statement so the runner's migration transaction stays intact.
    for statement in sql.split(";"):
        if statement.strip():
            conn.execute(statement)


def _copy_back(conn: sqlite3.Connection, table: str) -> None:
    kept = set(_columns(conn, table))
    shared = ",".join(f'"{name}"' for name in _columns(conn, f"{table}_138") if name in kept)
    conn.execute(f"INSERT INTO {table} ({shared}) SELECT {shared} FROM {table}_138")
    conn.execute(f"DROP TABLE {table}_138")


def _next_run_required(conn: sqlite3.Connection) -> bool:
    return any(row[1] == "next_run_at" and row[3] for row in conn.execute(
        "PRAGMA table_info(automation_schedules)"))


def migrate_138_to_139(conn: sqlite3.Connection) -> None:
    rebuilt = []
    for table, column in _NEW_COLUMNS.items():
        present = _columns(conn, table)
        if present and column not in present:
            conn.execute(f"ALTER TABLE {table} RENAME TO {table}_138")
            rebuilt.append(table)
    _create(conn, EVENT_RULE_SCHEMA_SQL)
    for table in rebuilt:
        _copy_back(conn, table)
    _create(conn, EVENT_RULE_SCHEMA_SQL)  # the renamed tables kept the index names until they were dropped
    if _next_run_required(conn):
        conn.execute("ALTER TABLE automation_schedules RENAME TO automation_schedules_138")
        _create(conn, AUTOMATION_SCHEDULES_SQL)
        _copy_back(conn, "automation_schedules")
        _create(conn, AUTOMATION_SCHEDULES_SQL)
