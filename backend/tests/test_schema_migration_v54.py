"""Schema v54 historically added the unbound-human reply-window indexes."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION

_NEW_INDEXES = {"idx_messages_unbound_human_turn"}


def _index_names(db: sqlite3.Connection) -> set[str]:
    return {
        row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name IN (?)",
            tuple(sorted(_NEW_INDEXES)),
        )
    }


def test_fresh_schema_is_the_current_version() -> None:
    assert SCHEMA_VERSION == 115


def test_historical_migration_adds_the_index_for_v54_replay() -> None:
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        assert not _index_names(db)
        db.execute("PRAGMA user_version=53")
        db.commit()

        result = migrate_to_current(db, 54)

        assert (result.migrated, result.from_version, result.to_version) == (True, 53, 54)
        assert _index_names(db) == _NEW_INDEXES
        assert not migrate_to_current(db, 54).migrated
