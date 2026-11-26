"""The approval record is installed on new and existing databases."""

import sqlite3

from infra.approval_schema import APPROVAL_SCHEMA_SQL
from infra.database_migrations import MIGRATIONS
from infra.migration_209_to_210_artifact_approvals import migrate_209_to_210


def _shape(db: sqlite3.Connection) -> list[tuple]:
    return list(db.execute("PRAGMA table_info(artifact_approvals)"))


def test_migration_matches_fresh_schema_and_is_idempotent():
    fresh = sqlite3.connect(":memory:")
    upgraded = sqlite3.connect(":memory:")
    try:
        fresh.executescript(APPROVAL_SCHEMA_SQL)
        assert MIGRATIONS[209] is migrate_209_to_210
        migrate_209_to_210(upgraded)
        migrate_209_to_210(upgraded)
        assert _shape(upgraded) == _shape(fresh)
        assert {row[1] for row in upgraded.execute("PRAGMA index_list(artifact_approvals)")} == {
            row[1] for row in fresh.execute("PRAGMA index_list(artifact_approvals)")
        }
    finally:
        fresh.close()
        upgraded.close()
