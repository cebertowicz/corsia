"""Add the agent-written event rule table and its outcome record."""
from __future__ import annotations

import sqlite3

from infra.event_rule_schema import EVENT_RULE_SCHEMA_SQL


def migrate_135_to_136(conn: sqlite3.Connection) -> None:
    # The fresh-install DDL, statement by statement so the runner's migration
    # transaction stays intact. Every statement is IF NOT EXISTS.
    for statement in EVENT_RULE_SCHEMA_SQL.split(";"):
        if statement.strip():
            conn.execute(statement)
