"""Schema v62 removes the unreadable unbound-human metadata index."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION

_INDEX = "idx_messages_unbound_human_unreadable"


def _has_index(db) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (_INDEX,),
    ).fetchone() is not None


def test_fresh_schema_does_not_create_the_removed_index():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        assert SCHEMA_VERSION == 159
        assert not _has_index(db)


def test_startup_migration_drops_the_removed_index(tmp_path):
    path = tmp_path / "unreadable-index.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        db.execute(f"CREATE INDEX {_INDEX} ON messages(thread_id)")
        db.execute("PRAGMA user_version=61")
        db.commit()

    with closing(sqlite3.connect(path)) as db:
        result = migrate_to_current(db, 62)
        assert (result.migrated, result.from_version, result.to_version) == (True, 61, 62)
        assert not _has_index(db)
        assert not migrate_to_current(db, 62).migrated
