"""Schema v70 removes the index used only by reply-window projection."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION

_INDEX = "idx_messages_unbound_human_turn"
_V69_INDEX_SQL = """
    CREATE INDEX idx_messages_unbound_human_turn
        ON messages(thread_id, json_extract(metadata, '$.turn_id'))
        WHERE role='user'
          AND active_turn_id IS NULL
          AND json_valid(metadata)
"""


def _has_index(db: sqlite3.Connection) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (_INDEX,)
    ).fetchone() is not None


def test_fresh_schema_does_not_create_the_removed_index() -> None:
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)

        assert SCHEMA_VERSION >= 70
        assert not _has_index(db)


def test_startup_migration_drops_the_v69_index_idempotently() -> None:
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        db.execute(_V69_INDEX_SQL)
        db.execute("PRAGMA user_version=69")
        db.commit()

        result = migrate_to_current(db, 70)

        assert (result.migrated, result.from_version, result.to_version) == (True, 69, 70)
        assert not _has_index(db)
        assert not migrate_to_current(db, 70).migrated
