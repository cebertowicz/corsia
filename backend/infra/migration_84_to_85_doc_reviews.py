"""Schema 84 -> 85: review requests linked to reviewer plan tasks."""
from __future__ import annotations

import sqlite3

from infra.doc_review_schema import DOC_REVIEW_SCHEMA_SQL


def migrate_84_to_85(conn: sqlite3.Connection) -> None:
    """Apply DDL without committing the startup migration's transaction."""
    for statement in DOC_REVIEW_SCHEMA_SQL.split(";"):
        if statement.strip():
            conn.execute(statement)
