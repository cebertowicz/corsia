"""Schema 123 -> 124: an existing database converges on the current ``threads`` shape."""

from __future__ import annotations

import sqlite3
from contextlib import closing

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_123_to_124_drop_title_source import migrate_123_to_124
from infra.schema import SCHEMA_VERSION


def _threads_columns(db: sqlite3.Connection) -> list[tuple[str, str]]:
    return [(row[1], row[2]) for row in db.execute("PRAGMA table_info(threads)")]


def _fresh() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    return db


def _wound_back_to_v123() -> sqlite3.Connection:
    """A fresh current database wound back to its schema-123 shape, with one thread."""
    db = _fresh()
    db.execute("ALTER TABLE threads ADD COLUMN title_source TEXT DEFAULT 'auto'")
    db.execute("PRAGMA user_version=123")
    db.execute(
        "INSERT INTO threads(id,user_id,title,title_source,created_at,updated_at) "
        "VALUES ('t1','u','Fixing the picker','auto','t','t')"
    )
    db.commit()
    return db


def test_the_step_is_registered_at_123() -> None:
    assert SCHEMA_VERSION == 165
    assert MIGRATIONS[123] is migrate_123_to_124


def test_the_step_matches_a_fresh_schema_and_keeps_every_thread() -> None:
    with closing(_fresh()) as fresh, closing(_wound_back_to_v123()) as db:
        assert _threads_columns(db) != _threads_columns(fresh)

        migrate_123_to_124(db)

        assert _threads_columns(db) == _threads_columns(fresh)
        assert db.execute("SELECT id,title FROM threads").fetchall() == [("t1", "Fixing the picker")]


def test_the_step_is_safe_to_run_again() -> None:
    with closing(_fresh()) as fresh, closing(_wound_back_to_v123()) as db:
        migrate_123_to_124(db)
        migrate_123_to_124(db)
        assert _threads_columns(db) == _threads_columns(fresh)


def test_the_runner_takes_a_123_database_to_current_and_stays_there() -> None:
    with closing(_fresh()) as fresh, closing(_wound_back_to_v123()) as db:
        result = migrate_to_current(db, SCHEMA_VERSION)

        assert (result.migrated, result.from_version, result.to_version) == (True, 123, SCHEMA_VERSION)
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert _threads_columns(db) == _threads_columns(fresh)
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
