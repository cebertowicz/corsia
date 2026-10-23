"""Schema v65 removes unused legacy session tables."""

from __future__ import annotations

import sqlite3
from contextlib import closing

from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION


_LEGACY_TABLES = ("activity_sessions", "session_summaries")


def _historical_guidance_loads(db: sqlite3.Connection) -> None:
    """Add the v47 table that a real v64 database already contains."""
    from infra.migration_46_to_47_guidance_manifests import _GUIDANCE_47_DDL

    for statement in _GUIDANCE_47_DDL:
        db.execute(statement)


def _table_names(db: sqlite3.Connection) -> set[str]:
    """Return any legacy session table still present in this database."""
    placeholders = ",".join("?" for _ in _LEGACY_TABLES)
    return {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            f"({placeholders})",
            _LEGACY_TABLES,
        )
    }


def test_startup_migration_removes_legacy_session_tables_idempotently() -> None:
    with closing(sqlite3.connect(":memory:")) as db:
        db.executescript("""
            CREATE TABLE activity_sessions (id TEXT PRIMARY KEY);
            CREATE TABLE session_summaries (id TEXT PRIMARY KEY);
            PRAGMA user_version=64;
        """)
        _historical_guidance_loads(db)
        assert _table_names(db) == set(_LEGACY_TABLES)

        result = migrate_to_current(db, SCHEMA_VERSION)

        assert SCHEMA_VERSION == 143
        assert (result.migrated, result.from_version, result.to_version) == (True, 64, SCHEMA_VERSION)
        assert _table_names(db) == set()
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
