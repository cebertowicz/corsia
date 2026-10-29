"""The measure file migration is additive and rolls back through startup."""
import sqlite3
from contextlib import closing

import pytest

from infra.database_migrations import MIGRATIONS, MigrationError, migrate_to_current


def old_database():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE measure_definitions(owner_thread_id TEXT, file_path TEXT, file_sha256 TEXT, synced_at TEXT)")
    db.execute("CREATE TABLE sentinel(id TEXT PRIMARY KEY)")
    db.execute("INSERT INTO sentinel VALUES ('preserved')")
    db.execute("PRAGMA user_version=164")
    db.commit()
    return db


def test_adds_version_attribute_without_changing_existing_rows():
    with closing(old_database()) as db:
        db.executemany("INSERT INTO measure_definitions VALUES (?,?,?,?)", [
            ('owner', '/a', 'old', '2026-01-01'), ('owner', '/a', 'new', '2026-01-02'),
            ('other', '/a', 'other', '2026-01-01'), ('owner', '/b', 'b', '2026-01-01')])
        db.commit()
        migrate_to_current(db, 165)
        assert db.execute("SELECT id FROM sentinel").fetchone() == ("preserved",)
        assert db.execute("SELECT version_attr FROM measure_definitions").fetchall() == [(None,)] * 4
        assert db.execute("PRAGMA user_version").fetchone()[0] == 165


def test_failure_rolls_back_column_and_version(monkeypatch):
    original = MIGRATIONS[164]

    def fail_after_create(db):
        original(db)
        raise RuntimeError("injected migration failure")

    monkeypatch.setitem(MIGRATIONS, 164, fail_after_create)
    with closing(old_database()) as db:
        db.execute("INSERT INTO measure_definitions VALUES ('owner','/a','hash','2026-01-01')")
        db.commit()
        before = list(db.iterdump())
        with pytest.raises(MigrationError, match="injected migration failure"):
            migrate_to_current(db, 165)
        assert list(db.iterdump()) == before
        assert db.execute("PRAGMA user_version").fetchone()[0] == 164
