"""The measure file migration is additive and rolls back through startup."""
import sqlite3
from contextlib import closing

import pytest

from infra.database_migrations import MIGRATIONS, MigrationError, migrate_to_current


def old_database():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE sentinel(id TEXT PRIMARY KEY)")
    db.execute("INSERT INTO sentinel VALUES ('preserved')")
    db.execute("PRAGMA user_version=162")
    db.commit()
    return db


def test_adds_metadata_table_without_changing_existing_tables():
    with closing(old_database()) as db:
        migrate_to_current(db, 163)
        assert db.execute("SELECT id FROM sentinel").fetchone() == ("preserved",)
        assert db.execute("SELECT version_attr FROM measure_files").fetchall() == []
        assert db.execute("PRAGMA user_version").fetchone()[0] == 163


def test_failure_rolls_back_metadata_table_and_version(monkeypatch):
    original = MIGRATIONS[162]

    def fail_after_create(db):
        original(db)
        raise RuntimeError("injected migration failure")

    monkeypatch.setitem(MIGRATIONS, 162, fail_after_create)
    with closing(old_database()) as db:
        before = list(db.iterdump())
        with pytest.raises(MigrationError, match="injected migration failure"):
            migrate_to_current(db, 163)
        assert list(db.iterdump()) == before
        assert db.execute("PRAGMA user_version").fetchone()[0] == 162
