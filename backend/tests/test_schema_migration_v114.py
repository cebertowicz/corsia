"""Schema 113 -> 114 durable Update operation migration."""

from __future__ import annotations

import sqlite3

from infra.database_migrations import migrate_to_current
from infra.migration_46_to_47_guidance_manifests import _GUIDANCE_47_DDL
from infra.schema import SCHEMA_VERSION


def test_v114_adds_update_operation_columns_to_existing_database(tmp_path) -> None:
    path = tmp_path / "restart.db"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE self_restart_requests (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, requested_at TEXT NOT NULL
            );
            INSERT INTO self_restart_requests VALUES ('operation', 'complete', 'now');
            PRAGMA user_version=113;
        """)
        connection.execute(_GUIDANCE_47_DDL[1])
    with sqlite3.connect(path) as connection:
        result = migrate_to_current(connection, SCHEMA_VERSION)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(self_restart_requests)")}
        row = connection.execute(
            "SELECT kind,trigger,target_version,drain_total,failure_code,"
            "failure_detail,conductor_told_at,changes,outcome_seen_at FROM self_restart_requests"
        ).fetchone()
    assert result.migrated is True
    assert {"kind", "trigger", "target_version", "drain_total", "failure_code", "failure_detail", "conductor_told_at", "changes", "outcome_seen_at"} <= columns
    assert row == (None, None, None, None, None, None, None, None, None)
