"""Schema 67 → 68: durable managed-process handles and retirements."""

from __future__ import annotations

import sqlite3

from infra.managed_process_schema import MANAGED_PROCESS_DDL


def migrate_67_to_68(conn: sqlite3.Connection) -> None:
    for statement in MANAGED_PROCESS_DDL:
        conn.execute(statement)
