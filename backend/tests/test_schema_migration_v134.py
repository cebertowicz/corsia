"""Schema v134 records when a suggestion's author was told its text changed."""

from __future__ import annotations

import sqlite3

from infra.database_migrations import MIGRATIONS
from infra.migration_133_to_134_doc_stranded_suggestions import migrate_133_to_134
from infra.schema import SCHEMA_SQL, SCHEMA_VERSION


def _columns(db: sqlite3.Connection) -> list[str]:
    return [row[1] for row in db.execute("PRAGMA table_info(doc_comments)")]


def test_registered_as_the_current_step():
    assert SCHEMA_VERSION == 134
    assert MIGRATIONS[133] is migrate_133_to_134


def test_adds_the_nullable_column_once_and_keeps_rows():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE doc_comments (id TEXT PRIMARY KEY, body TEXT)")
    db.execute("INSERT INTO doc_comments VALUES ('c1','text')")

    migrate_133_to_134(db)
    migrate_133_to_134(db)  # a second start changes nothing

    assert _columns(db)[-1] == "stranded_notified_at"
    assert db.execute("SELECT body, stranded_notified_at FROM doc_comments").fetchone() == ("text", None)


def test_a_fresh_schema_lists_the_same_column_last():
    fresh = sqlite3.connect(":memory:")
    fresh.executescript(SCHEMA_SQL)

    assert _columns(fresh)[-1] == "stranded_notified_at"


def test_a_database_without_doc_comments_passes():
    db = sqlite3.connect(":memory:")

    migrate_133_to_134(db)

    assert db.execute("SELECT 1 FROM sqlite_master WHERE name='doc_comments'").fetchone() is None
