"""Schema v135 records whether a suggestion's decision settled several suggestions at once."""

from __future__ import annotations

import sqlite3

from infra.database_migrations import MIGRATIONS
from infra.migration_134_to_135_doc_decision_bulk import migrate_134_to_135
from infra.schema import SCHEMA_SQL, SCHEMA_VERSION


def _columns(db: sqlite3.Connection) -> list[str]:
    return [row[1] for row in db.execute("PRAGMA table_info(doc_comments)")]


def test_registered_as_the_current_step():
    assert SCHEMA_VERSION >= 135
    assert MIGRATIONS[134] is migrate_134_to_135


def test_adds_the_nullable_column_once_and_keeps_rows_unknown():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE doc_comments (id TEXT PRIMARY KEY, body TEXT, decision TEXT)")
    db.execute("INSERT INTO doc_comments VALUES ('c1','text','accepted')")

    migrate_134_to_135(db)
    migrate_134_to_135(db)  # a second start changes nothing

    assert _columns(db)[-1] == "decided_in_bulk"
    # A decision made before the flag existed was never recorded as bulk or single, so it reads NULL.
    assert db.execute("SELECT body, decided_in_bulk FROM doc_comments").fetchone() == ("text", None)


def test_a_fresh_schema_lists_the_same_column_last():
    fresh = sqlite3.connect(":memory:")
    fresh.executescript(SCHEMA_SQL)

    assert _columns(fresh)[-1] == "author_user_id"


def test_a_database_without_doc_comments_passes():
    db = sqlite3.connect(":memory:")

    migrate_134_to_135(db)

    assert db.execute("SELECT 1 FROM sqlite_master WHERE name='doc_comments'").fetchone() is None
