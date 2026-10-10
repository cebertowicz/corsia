"""Schema 97 -> 98: record each call reasoning effort and limit-window history.

Additive: one nullable column on ``llm_usage`` and one new table with its
index. Existing rows keep a NULL effort (it was never recorded and cannot be
recovered). Runs inside the runner transaction; it never commits.
"""

from __future__ import annotations

from infra.limit_history_schema import LIMIT_HISTORY_SCHEMA_SQL


def migrate_97_to_98(conn) -> None:
    # A database may already have the column from the current DDL.
    # A ladder run from an old stub database may have no ledger table at all.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(llm_usage)")}
    if columns and "reasoning_effort" not in columns:
        conn.execute("ALTER TABLE llm_usage ADD COLUMN reasoning_effort TEXT")
    for statement in LIMIT_HISTORY_SCHEMA_SQL.split(";"):
        if statement.strip():
            conn.execute(statement)
