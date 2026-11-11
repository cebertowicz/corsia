"""Schema 182 -> 183: the cut that fences a thread's pre-move history (design row R3).

The column is the only schema this slice adds, and history fencing reads nothing
else, so these tests are the whole step: it registers at 182, adds the column to an
existing database, runs twice, and leaves a fresh schema and a migrated one alike.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS
from infra.migration_182_to_183_local_only_cut import migrate_182_to_183
from infra.schema import SCHEMA_VERSION


def _fresh() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    return db


def _columns(db: sqlite3.Connection) -> dict[str, tuple]:
    return {row[1]: (row[2], row[3], row[4], row[5]) for row in db.execute("PRAGMA table_info(threads)")}


def test_the_step_is_registered_at_182() -> None:
    assert SCHEMA_VERSION == 188
    assert MIGRATIONS[182] is migrate_182_to_183


def test_the_step_adds_the_column_a_fresh_schema_has_and_runs_twice() -> None:
    with closing(_fresh()) as fresh, closing(_fresh()) as db:
        db.execute("ALTER TABLE threads DROP COLUMN local_only_cut_seq")
        db.execute("PRAGMA user_version=182")
        assert "local_only_cut_seq" not in _columns(db)

        migrate_182_to_183(db)
        migrate_182_to_183(db)

        # A migration appends the column and a fresh schema declares it in place, so
        # the shapes match by name and definition rather than by position.
        assert _columns(db) == _columns(fresh)


def test_an_existing_thread_starts_with_no_cut() -> None:
    """A thread that never left a Local only project keeps reading all of its history."""
    with closing(_fresh()) as db:
        db.execute("ALTER TABLE threads DROP COLUMN local_only_cut_seq")
        db.execute(
            "INSERT INTO threads (id,user_id,title,created_at,updated_at)"
            " VALUES ('t','u','T','t','t')"
        )
        migrate_182_to_183(db)
        assert db.execute("SELECT local_only_cut_seq FROM threads WHERE id='t'").fetchone()[0] is None


def test_the_step_skips_a_database_without_threads() -> None:
    with closing(sqlite3.connect(":memory:")) as db:
        migrate_182_to_183(db)
