"""Schema v36 builds the checkpoint-entry search index atomically."""

from __future__ import annotations

from contextlib import closing
import sqlite3

import pytest

from infra import database_migrations as migrations


def _entry_search_objects(db: sqlite3.Connection) -> list[tuple[str, str]]:
    return db.execute(
        "SELECT type, name FROM sqlite_master "
        "WHERE name GLOB 'checkpoint_entry_search*' ORDER BY type, name"
    ).fetchall()


def _make_v35_database(path) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
        db.execute(
            "CREATE TABLE checkpoints ("
            "id TEXT NOT NULL, generation_order INTEGER PRIMARY KEY, status TEXT NOT NULL)"
        )
        db.execute("PRAGMA user_version=35")
        db.commit()


def test_v36_migration_builds_the_entry_search_index(tmp_path):
    """The successful path builds both the index table and FTS companion."""
    path = tmp_path / "legacy-v35.db"
    _make_v35_database(path)

    with closing(sqlite3.connect(path)) as db:
        result = migrations.migrate_to_current(db, 36)

        assert (result.migrated, result.from_version, result.to_version) == (True, 35, 36)
        assert ("table", "checkpoint_entry_search") in _entry_search_objects(db)
        assert ("table", "checkpoint_entry_search_fts") in _entry_search_objects(db)


def test_failed_v36_backfill_rolls_back_its_schema(tmp_path, monkeypatch):
    """A backfill failure leaves a v35 database unchanged for a safe retry."""
    path = tmp_path / "legacy-v35.db"
    _make_v35_database(path)

    def partially_backfill(db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO checkpoint_entry_search("
            "thread_id, history_epoch, entry_id, revision, field, search_text, "
            "first_generation_order, last_generation_order) "
            "VALUES ('thread', 0, 1, 1, 'facts', 'partial', 1, 1)"
        )
        raise RuntimeError("injected v36 backfill failure")

    monkeypatch.setattr(migrations, "backfill_checkpoint_entry_search", partially_backfill)

    with closing(sqlite3.connect(path)) as db:
        with pytest.raises(migrations.MigrationError, match="injected v36 backfill failure"):
            migrations.migrate_to_current(db, 36)

        assert db.execute("PRAGMA user_version").fetchone() == (35,)
        assert _entry_search_objects(db) == []
        assert db.execute("SELECT name FROM sqlite_master WHERE name='checkpoints'").fetchall() == [
            ("checkpoints",),
        ]
