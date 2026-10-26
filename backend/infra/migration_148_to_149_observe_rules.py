"""Schema 148 -> 149, observe rules: ``event_rules.kind``, ``tool``, ``argv_prefix`` and ``item_type``,
the ``registered`` and ``not_registered`` outcomes of ``event_route_decisions``, and the shipped rule
sets' observe rows.

SQLite can't widen a CHECK or free a NOT NULL in place, so each table that isn't in its new shape is
rebuilt from the fresh-install DDL and its rows copied over (every existing rule becomes a ``route``
rule). Guarded: a table already in its new shape is left alone, and the shipped rows are inserted once.
"""
from __future__ import annotations

import sqlite3

from infra.event_rule_schema import EVENT_RULE_SCHEMA_SQL, SHIPPED_OBSERVE_RULES_SQL

_OLD = "_148"


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _definition(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row[0] if row else ""


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
    shared = ",".join(f'"{name}"' for name in _columns(conn, f"{table}{_OLD}") if name in kept)
    conn.execute(f"INSERT INTO {table} ({shared}) SELECT {shared} FROM {table}{_OLD}")
    conn.execute(f"DROP TABLE {table}{_OLD}")


def migrate_148_to_149(conn: sqlite3.Connection) -> None:
    stale = {
        "event_rules": "kind" not in _columns(conn, "event_rules"),
        "event_route_decisions": "'not_registered'" not in _definition(conn, "event_route_decisions"),
    }
    rebuilt = [table for table, old in stale.items() if old and _columns(conn, table)]
    for table in rebuilt:
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}{_OLD}")
    _create(conn, EVENT_RULE_SCHEMA_SQL)
    for table in rebuilt:
        _copy_back(conn, table)
    _create(conn, EVENT_RULE_SCHEMA_SQL)  # the renamed tables kept the index names until they were dropped
    _create(conn, SHIPPED_OBSERVE_RULES_SQL)
