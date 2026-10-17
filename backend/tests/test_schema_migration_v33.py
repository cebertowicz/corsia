"""Schema v33 adds provider-reset identity to the existing liveness row."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION
from tests.schema_rewind import rewind_below_v42, rewind_to_v88_policy


_RESET_COLUMNS = {"provider_reset_at", "provider_reset_turn_id"}


def _columns(db) -> set[str]:
    return {
        str(row[1])
        for row in db.execute("PRAGMA table_info(plan_liveness_wakes)")
    }


def test_fresh_schema_has_provider_reset_columns():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        assert SCHEMA_VERSION == 125
        assert _RESET_COLUMNS <= _columns(db)


def test_startup_migration_adds_provider_reset_columns(tmp_path):
    path = tmp_path / "legacy-plan-liveness-v32.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        db.execute("ALTER TABLE plan_liveness_wakes DROP COLUMN provider_reset_at")
        db.execute("ALTER TABLE plan_liveness_wakes DROP COLUMN provider_reset_turn_id")
        rewind_below_v42(db)
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("PRAGMA user_version=32")
        db.commit()

    with closing(sqlite3.connect(path)) as db:
        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (True, 32, SCHEMA_VERSION)
        assert _RESET_COLUMNS <= _columns(db)
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
