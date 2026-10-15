"""Schema v34 adds the bounded thread-checkpoint listing index."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION
from tests.schema_rewind import rewind_below_v42, rewind_to_v88_policy


def _indexes(db) -> set[str]:
    return {str(row[1]) for row in db.execute("PRAGMA index_list(checkpoints)")}


def test_fresh_schema_has_checkpoint_listing_index():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        assert SCHEMA_VERSION == 120
        assert "checkpoints_listing" in _indexes(db)


def test_startup_migration_adds_checkpoint_listing_index(tmp_path):
    path = tmp_path / "legacy-checkpoint-listing-v33.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        db.execute("DROP INDEX checkpoints_listing")
        rewind_below_v42(db)
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("PRAGMA user_version=33")
        db.commit()

    with closing(sqlite3.connect(path)) as db:
        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (True, 33, SCHEMA_VERSION)
        assert "checkpoints_listing" in _indexes(db)
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
