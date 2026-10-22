"""Schema 126 -> 127: the corrections scan's index on user comments by time lands on an existing database."""

from __future__ import annotations

import sqlite3
from contextlib import closing

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS
from infra.migration_126_to_127_user_comments_index import migrate_126_to_127
from infra.schema import SCHEMA_VERSION

_INDEX = "idx_doc_comments_user_created"


def _fresh() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    return db


def _index_sql(db: sqlite3.Connection) -> str | None:
    row = db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (_INDEX,)).fetchone()
    return row[0] if row else None


def test_the_step_is_registered_at_126() -> None:
    assert SCHEMA_VERSION == 135
    assert MIGRATIONS[126] is migrate_126_to_127


def test_the_step_adds_the_index_a_fresh_schema_has_and_runs_twice() -> None:
    with closing(_fresh()) as fresh, closing(_fresh()) as db:
        db.execute(f"DROP INDEX {_INDEX}")
        db.execute("PRAGMA user_version=126")
        assert _index_sql(db) is None

        migrate_126_to_127(db)
        migrate_126_to_127(db)

        assert _index_sql(db) == _index_sql(fresh) is not None


def test_the_step_skips_a_database_without_comments() -> None:
    with closing(sqlite3.connect(":memory:")) as db:
        migrate_126_to_127(db)
        assert _index_sql(db) is None
