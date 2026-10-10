"""Schema 98 -> 99: the marker a plan write leaves for its after-commit status.

Additive: one new table. No plan is dirty at the cutover, because every write
before it journaled its status in its own transaction. Runs inside the runner
transaction; it never commits.
"""

from __future__ import annotations

from infra.plan_status_schema import PLAN_STATUS_DIRTY_SCHEMA_SQL


def migrate_98_to_99(conn) -> None:
    for statement in PLAN_STATUS_DIRTY_SCHEMA_SQL.split(";"):
        if statement.strip():
            conn.execute(statement)
