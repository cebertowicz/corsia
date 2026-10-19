"""Schema 67 migration adds managed-process handles and preserves existing state."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION


def _tables(db: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def test_schema_67_upgrades_to_current_without_changing_existing_state() -> None:
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        db.execute("DROP TABLE managed_process_retirements")
        db.execute("DROP TABLE managed_process_handles")
        db.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES ('sentinel','kept','t')"
        )
        db.execute("PRAGMA user_version=67")
        db.commit()

        result = migrate_to_current(db, 68)

        assert SCHEMA_VERSION == 128
        assert (result.migrated, result.from_version, result.to_version) == (
            True,
            67,
            68,
        )
        assert {
            "managed_process_handles",
            "managed_process_retirements",
        } <= _tables(db)
        assert db.execute(
            "SELECT value FROM settings WHERE key='sentinel'"
        ).fetchone()[0] == "kept"
        assert db.execute("PRAGMA user_version").fetchone()[0] == 68
