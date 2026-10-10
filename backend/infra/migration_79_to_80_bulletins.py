"""Schema 79 -> 80: the workspace-local System bulletin ledger."""
from __future__ import annotations

import sqlite3

from infra.bulletin_schema import BULLETIN_SCHEMA_SQL


def migrate_79_to_80(conn: sqlite3.Connection) -> None:
    """Create the ledger from the fresh-install DDL, without executescript's implicit commit."""
    for statement in BULLETIN_SCHEMA_SQL.split(";"):
        if statement.strip():
            conn.execute(statement)
