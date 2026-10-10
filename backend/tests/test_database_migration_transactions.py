"""Migration failure cannot commit foreign work or leak pending schema changes."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from infra import database_migrations as migrations


class _CommitFailure(sqlite3.Connection):
    """Reject one actual commit boundary, whether issued as SQL or a method."""

    reject_commit = False

    def _check_commit(self):
        if self.reject_commit:
            self.reject_commit = False
            raise sqlite3.OperationalError("injected migration commit failure")

    def execute(self, sql, parameters=()):
        if sql.strip().upper() == "COMMIT":
            self._check_commit()
        return super().execute(sql, parameters)

    def commit(self):
        self._check_commit()
        return super().commit()


def _add_table(db):
    db.execute("CREATE TABLE migrated (value TEXT)")
    db.execute("INSERT INTO migrated VALUES ('new schema')")


def _fail_step(db):
    _add_table(db)
    raise ValueError("injected migration step failure")


@pytest.fixture
def migration_database(tmp_path, monkeypatch):
    path = tmp_path / "migration.db"
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE original (value TEXT)")
        db.execute("INSERT INTO original VALUES ('committed')")
        db.execute("PRAGMA user_version=1")
        db.commit()
    monkeypatch.setattr(migrations, "MIGRATIONS", {1: _add_table})
    return path


@pytest.mark.parametrize("isolation", [None, "IMMEDIATE"])
def test_migration_does_not_commit_an_existing_owner(migration_database, isolation):
    """Changing isolation before checking ownership must not commit caller data."""
    db = sqlite3.connect(migration_database, isolation_level=isolation)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=71")
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO original VALUES ('caller pending')")

        with pytest.raises(migrations.MigrationError, match="clean.*connection"):
            migrations.migrate_to_current(db, 2)

        assert db.in_transaction
        assert db.isolation_level == isolation
        assert db.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert db.execute("PRAGMA busy_timeout").fetchone() == (71,)
        assert db.execute("SELECT value FROM original ORDER BY rowid").fetchall() == [
            ("committed",), ("caller pending",),
        ]
        assert list(migration_database.parent.glob("*.pre-migration-*.bak")) == []
        with closing(sqlite3.connect(migration_database)) as reader:
            assert reader.execute("SELECT value FROM original").fetchall() == [
                ("committed",),
            ]
            assert reader.execute("PRAGMA user_version").fetchone() == (1,)
        db.rollback()
        assert db.execute("SELECT value FROM original").fetchall() == [("committed",)]
    finally:
        db.close()


@pytest.mark.parametrize("isolation", [None, "DEFERRED"])
def test_failed_commit_cannot_escape_through_a_later_commit(
    migration_database, isolation,
):
    """A failed migration commit must roll back DDL before returning the connection."""
    db = sqlite3.connect(
        migration_database, isolation_level=isolation, factory=_CommitFailure,
    )
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=71")
        db.reject_commit = True
        with pytest.raises(sqlite3.OperationalError, match="injected migration commit"):
            migrations.migrate_to_current(db, 2)

        assert not db.in_transaction
        assert db.isolation_level == isolation
        assert db.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert db.execute("PRAGMA busy_timeout").fetchone() == (71,)
        db.execute("INSERT INTO original VALUES ('later caller')")
        db.commit()
        with closing(sqlite3.connect(migration_database)) as reader:
            assert reader.execute("PRAGMA user_version").fetchone() == (1,)
            assert reader.execute(
                "SELECT name FROM sqlite_master WHERE name='migrated'",
            ).fetchall() == []
            assert reader.execute("SELECT value FROM original ORDER BY rowid").fetchall() == [
                ("committed",), ("later caller",),
            ]
        backups = list(migration_database.parent.glob("*.pre-migration-*.bak"))
        assert len(backups) == 1
        with closing(sqlite3.connect(backups[0])) as backup:
            assert backup.execute("PRAGMA user_version").fetchone() == (1,)
            assert backup.execute("SELECT value FROM original").fetchall() == [
                ("committed",),
            ]
    finally:
        db.close()


@pytest.mark.parametrize("isolation", [None, "", "EXCLUSIVE"])
@pytest.mark.parametrize("fail", [False, True])
def test_connection_settings_survive_success_and_step_failure(
    migration_database, monkeypatch, isolation, fail,
):
    """The transaction replacement must preserve caller configuration and usability."""
    if fail:
        monkeypatch.setattr(migrations, "MIGRATIONS", {1: _fail_step})
    db = sqlite3.connect(migration_database, isolation_level=isolation)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=71")
        if fail:
            with pytest.raises(migrations.MigrationError, match="injected migration step"):
                migrations.migrate_to_current(db, 2)
        else:
            assert migrations.migrate_to_current(db, 2).migrated

        assert not db.in_transaction
        assert db.isolation_level == isolation
        assert db.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert db.execute("PRAGMA busy_timeout").fetchone() == (71,)
        assert db.execute("PRAGMA user_version").fetchone() == ((1,) if fail else (2,))
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE name='migrated'",
        ).fetchall() == ([] if fail else [("migrated",)])
    finally:
        db.close()
