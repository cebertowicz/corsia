"""Schema v44 copies the system_context setting to workspace_context (#939)."""

from __future__ import annotations

from tests.schema_rewind import rewind_to_v88_policy

from contextlib import closing
import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION


def test_startup_migration_copies_the_setting_and_keeps_the_legacy_row(tmp_path):
    path = tmp_path / "legacy-workspace-context-v43.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        db.execute(
            "INSERT INTO settings (key, value, updated_at) "
            "VALUES ('system_context', '{\"bootloader_files\": []}', 't')"
        )
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("PRAGMA user_version=43")
        db.commit()

    with closing(sqlite3.connect(path)) as db:
        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (
            True, 43, SCHEMA_VERSION,
        )
        rows = dict(db.execute(
            "SELECT key, value FROM settings WHERE key IN "
            "('system_context', 'workspace_context')"
        ).fetchall())
        assert rows == {
            "system_context": '{"bootloader_files": []}',
            "workspace_context": '{"bootloader_files": []}',
        }
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
