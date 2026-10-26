"""Schema 104 -> 105: when a continuity entry was last reviewed."""
from __future__ import annotations

import sqlite3
from contextlib import closing

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_104_to_105_review_time import migrate_104_to_105
from infra.schema import SCHEMA_VERSION


def _columns(db) -> list[str]:
    return [row[1] for row in db.execute("PRAGMA table_info(continuity_entries)")]


def test_migration_registered_and_adds_the_review_time_column(tmp_path):
    assert SCHEMA_VERSION == 147 and MIGRATIONS[104] is migrate_104_to_105
    path = tmp_path / "v104.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        db.execute("ALTER TABLE continuity_entries DROP COLUMN reviewed_at")
        db.execute("PRAGMA user_version=104")
        db.commit()
        assert "reviewed_at" not in _columns(db)
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        assert db.execute("PRAGMA user_version").fetchone()[0] == 147
        assert "reviewed_at" in _columns(db)
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated


def test_migration_keeps_rows_and_is_safe_to_repeat(tmp_path):
    with closing(sqlite3.connect(tmp_path / "fresh.db")) as db:
        initialize_schema(db)
        before = _columns(db)
        migrate_104_to_105(db)
        assert _columns(db) == before
