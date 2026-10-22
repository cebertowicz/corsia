"""Schema 140 -> 141, rules that start workers: ``event_rules.workflow_id`` and ``request_identity_fact``, and the
start outcomes, request identity and worker of ``event_route_decisions``.

SQLite can't widen a CHECK in place, so both tables are rebuilt from the
fresh-install DDL and their rows copied over. Guarded: a table already in its
new shape is left alone. A script only event rules start is told apart by its
missing next run alone, so the ``rules`` spec it used to carry is cleared.
"""
from __future__ import annotations

import sqlite3

from infra.event_rule_schema import EVENT_RULE_SCHEMA_SQL

_NEW_COLUMNS = {"event_rules": "workflow_id", "event_route_decisions": "worker_thread_id"}


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _create(conn: sqlite3.Connection, sql: str) -> None:
    # Statement by statement so the runner's migration transaction stays intact; a piece
    # joins the next until SQLite reads it as complete, so a ';' in a comment can't split one.
    pending = ""
    for piece in sql.split(";"):
        pending += piece + ";"
        if sqlite3.complete_statement(pending):
            if pending.strip(" \n;"):
                conn.execute(pending)
            pending = ""


def _copy_back(conn: sqlite3.Connection, table: str) -> None:
    kept = set(_columns(conn, table))
    shared = ",".join(f'"{name}"' for name in _columns(conn, f"{table}_140") if name in kept)
    conn.execute(f"INSERT INTO {table} ({shared}) SELECT {shared} FROM {table}_140")
    conn.execute(f"DROP TABLE {table}_140")


def migrate_140_to_141(conn: sqlite3.Connection) -> None:
    rebuilt = []
    for table, column in _NEW_COLUMNS.items():
        present = _columns(conn, table)
        if present and column not in present:
            conn.execute(f"ALTER TABLE {table} RENAME TO {table}_140")
            rebuilt.append(table)
    _create(conn, EVENT_RULE_SCHEMA_SQL)
    for table in rebuilt:
        _copy_back(conn, table)
    _create(conn, EVENT_RULE_SCHEMA_SQL)  # the renamed tables kept the index names until they were dropped
    if _columns(conn, "automation_schedules"):
        conn.execute("UPDATE automation_schedules SET spec='' WHERE kind='script' AND spec='rules'")
