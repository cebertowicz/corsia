"""Schema v74 adds the time an installed checkpoint's history ends at (#3196)."""

from __future__ import annotations

from tests.schema_rewind import rewind_to_v88_policy

import sqlite3
from contextlib import closing

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION


def _columns(db) -> set[str]:
    return {str(row[1]) for row in db.execute("PRAGMA table_info(checkpoints)")}


def test_fresh_schema_has_the_checkpoint_through_at_column():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        assert SCHEMA_VERSION == 163
        assert "through_at" in _columns(db)


def test_startup_migration_adds_through_at(tmp_path):
    path = tmp_path / "legacy-checkpoint-through-at-v73.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        db.execute("DROP TABLE measure_files")
        # Wind the table back to its v73 shape: the column did not exist.
        db.execute("ALTER TABLE checkpoints DROP COLUMN through_at")
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("PRAGMA user_version=73")
        db.commit()

    with closing(sqlite3.connect(path)) as db:
        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (
            True, 73, SCHEMA_VERSION,
        )
        assert "through_at" in _columns(db)
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated