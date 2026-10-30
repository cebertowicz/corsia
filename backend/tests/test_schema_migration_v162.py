"""Task-start receipt migration keeps existing data and rolls back atomically."""
import sqlite3

import pytest

from infra.database_migrations import MIGRATIONS, MigrationError, migrate_to_current
from infra.migration_161_to_162_trial_receipts import migrate_161_to_162


def _old_database():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE guidance_loads (id TEXT PRIMARY KEY, thread_id TEXT, created_at TEXT)")
    db.execute("INSERT INTO guidance_loads VALUES ('receipt', 'thread', 'time')")
    db.execute("PRAGMA user_version=161")
    db.commit()
    return db


def test_receipt_migration_preserves_existing_rows():
    db = _old_database()
    try:
        assert MIGRATIONS[161] is migrate_161_to_162
        migrate_to_current(db, 162)
        assert db.execute("SELECT id, variant, omission_reason FROM guidance_loads").fetchall() == [("receipt", None, None)]
        assert db.execute("PRAGMA user_version").fetchone()[0] == 162
        assert not migrate_to_current(db, 162).migrated
    finally:
        db.close()


def test_receipt_migration_rolls_back_ddl_and_version_on_failure(monkeypatch):
    db = _old_database()
    try:
        before = list(db.iterdump())

        def fail_after_ddl(conn):
            migrate_161_to_162(conn)
            raise RuntimeError("injected failure")

        monkeypatch.setitem(MIGRATIONS, 161, fail_after_ddl)
        with pytest.raises(MigrationError, match="injected failure"):
            migrate_to_current(db, 162)
        assert [row[1] for row in db.execute("PRAGMA table_info(guidance_loads)")] == ["id", "thread_id", "created_at"]
        assert db.execute("PRAGMA user_version").fetchone()[0] == 161
        assert list(db.iterdump()) == before
    finally:
        db.close()
