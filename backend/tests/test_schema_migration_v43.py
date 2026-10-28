"""Schema v43 adds the frozen checkpoint snapshot columns (#1602)."""

from __future__ import annotations

from tests.schema_rewind import rewind_to_v88_policy

from contextlib import closing
import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION


def _columns(db) -> set[str]:
    return {str(row[1]) for row in db.execute("PRAGMA table_info(checkpoints)")}


def test_fresh_schema_has_checkpoint_snapshot_columns():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        assert SCHEMA_VERSION == 159
        assert {"artifact_snapshot_json", "plan_status_snapshot_json"} <= _columns(db)


def test_startup_migration_adds_checkpoint_snapshot_columns(tmp_path):
    path = tmp_path / "legacy-checkpoint-snapshots-v42.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        # Wind the table back to its v42 shape: the snapshot columns did not
        # exist, and a legacy installed row carries nothing to migrate.
        db.execute("ALTER TABLE checkpoints DROP COLUMN artifact_snapshot_json")
        db.execute("ALTER TABLE checkpoints DROP COLUMN plan_status_snapshot_json")
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("PRAGMA user_version=42")
        db.commit()

    with closing(sqlite3.connect(path)) as db:
        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (
            True, 42, SCHEMA_VERSION,
        )
        assert {"artifact_snapshot_json", "plan_status_snapshot_json"} <= _columns(db)
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
