"""The approval record is installed on new and existing databases."""

import sqlite3

from infra.approval_schema import APPROVAL_SCHEMA_SQL
from infra.database_migrations import MIGRATIONS
from infra.migration_211_to_212_artifact_approvals import migrate_211_to_212


def _shape(db: sqlite3.Connection) -> list[tuple]:
    return list(db.execute("PRAGMA table_info(artifact_approvals)"))


def test_migration_matches_fresh_schema_and_is_idempotent():
    fresh = sqlite3.connect(":memory:")
    upgraded = sqlite3.connect(":memory:")
    try:
        fresh.executescript(APPROVAL_SCHEMA_SQL)
        assert MIGRATIONS[211] is migrate_211_to_212
        migrate_211_to_212(upgraded)
        migrate_211_to_212(upgraded)
        assert _shape(upgraded) == _shape(fresh)
        assert {row[1] for row in upgraded.execute("PRAGMA index_list(artifact_approvals)")} == {
            row[1] for row in fresh.execute("PRAGMA index_list(artifact_approvals)")
        }
    finally:
        fresh.close()
        upgraded.close()


def test_database_upgrades_from_211_without_losing_existing_data():
    from infra.database import initialize_schema
    from infra.database_migrations import migrate_to_current

    db = sqlite3.connect(":memory:")
    try:
        initialize_schema(db)
        db.execute("DROP TABLE artifact_approvals")
        db.execute("INSERT INTO settings (key, value, updated_at) VALUES ('approval-migration-test', 'preserved', 'now')")
        db.execute("PRAGMA user_version=211")
        db.commit()
        migrate_to_current(db, 212)
        assert db.execute("PRAGMA user_version").fetchone()[0] == 212
        assert db.execute("SELECT value FROM settings WHERE key='approval-migration-test'").fetchone()[0] == 'preserved'
        assert _shape(db)
    finally:
        db.close()
