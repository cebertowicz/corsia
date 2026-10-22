"""Schema v69 drops the four never-written ``context_change_journal`` columns."""

from __future__ import annotations

import sqlite3
from contextlib import closing

from infra.context_change_schema import CONTEXT_CHANGE_SCHEMA_SQL
from infra.database_migrations import migrate_to_current
from infra.passive_changes import append_change
from infra.schema import SCHEMA_VERSION

_DROPPED = {"old_hash", "new_hash", "diff", "reason"}
_V68_JOURNAL = """
    CREATE TABLE context_change_journal (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        object_kind TEXT NOT NULL,
        object_id TEXT NOT NULL,
        change_kind TEXT NOT NULL,
        old_hash TEXT,
        new_hash TEXT,
        summary TEXT NOT NULL,
        diff TEXT,
        reason TEXT,
        details_json TEXT,
        actor_thread_id TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_context_changes_object
        ON context_change_journal(object_kind, object_id, seq);
"""


def _columns(db: sqlite3.Connection) -> set[str]:
    return {row[1] for row in db.execute("PRAGMA table_info(context_change_journal)")}


def test_startup_migration_drops_unwritten_columns_and_keeps_rows() -> None:
    with closing(sqlite3.connect(":memory:")) as db:
        db.executescript(_V68_JOURNAL + "PRAGMA user_version=68;")
        db.execute(
            "INSERT INTO context_change_journal "
            "(object_kind,object_id,change_kind,summary,details_json,created_at) "
            "VALUES ('k','o','updated','kept','{\"a\": 1}','2026-09-29T00:00:00Z')"
        )
        db.commit()
        assert _DROPPED <= _columns(db)

        result = migrate_to_current(db, 69)

        assert SCHEMA_VERSION == 137
        assert (result.migrated, result.from_version, result.to_version) == (True, 68, 69)
        assert not _DROPPED & _columns(db)
        row = db.execute(
            "SELECT summary, details_json FROM context_change_journal"
        ).fetchone()
        assert tuple(row) == ("kept", '{"a": 1}')
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE name='idx_context_changes_object'"
        ).fetchone()
        assert not migrate_to_current(db, 69).migrated


def test_fresh_schema_matches_migrated_schema_and_append_change_writes() -> None:
    with closing(sqlite3.connect(":memory:")) as db:
        db.executescript(CONTEXT_CHANGE_SCHEMA_SQL)
        assert not _DROPPED & _columns(db)
        seq = append_change(
            db, object_kind="k", object_id="o", change_kind="updated", summary="s"
        )
        assert seq == 1
