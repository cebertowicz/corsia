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
    db.execute("PRAGMA user_version=163")
    db.commit()
    return db


def test_adds_metadata_table_without_changing_existing_tables():
    with closing(old_database()) as db:
        db.executemany("INSERT INTO measure_definitions VALUES (?,?,?,?)", [
            ('owner', '/a', 'old', '2026-01-01'), ('owner', '/a', 'new', '2026-01-02'),
            ('other', '/a', 'other', '2026-01-01'), ('owner', '/b', 'b', '2026-01-01')])
        db.commit()
        migrate_to_current(db, 164)
        assert db.execute("SELECT id FROM sentinel").fetchone() == ("preserved",)
        assert db.execute("SELECT owner_thread_id,file_path,file_sha256,version_attr,synced_at FROM measure_files ORDER BY 1,2").fetchall() == [
            ('other', '/a', 'other', None, '2026-01-01'),
            ('owner', '/a', 'new', None, '2026-01-02'),
            ('owner', '/b', 'b', None, '2026-01-01')]
        assert db.execute("PRAGMA user_version").fetchone()[0] == 164


def test_failure_rolls_back_metadata_table_and_version(monkeypatch):
    original = MIGRATIONS[163]

    def fail_after_create(db):
        original(db)
        raise RuntimeError("injected migration failure")

    monkeypatch.setitem(MIGRATIONS, 163, fail_after_create)
    with closing(old_database()) as db:
        db.execute("INSERT INTO measure_definitions VALUES ('owner','/a','hash','2026-01-01')")
        db.commit()
        before = list(db.iterdump())
        with pytest.raises(MigrationError, match="injected migration failure"):
            migrate_to_current(db, 164)
        assert list(db.iterdump()) == before
        assert db.execute("PRAGMA user_version").fetchone()[0] == 163
