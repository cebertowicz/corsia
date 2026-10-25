"""Schema v24 renames artifact_registrations.node_retired_at to retired_at."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION


def _legacy_database(tmp_path):
    path = tmp_path / "rename-retired-at.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        # This is a v23 snapshot, one schema before the rename this migration
        # performs, so the fixture must still carry the pre-rename name.
        db.execute(
            "ALTER TABLE artifact_registrations RENAME COLUMN retired_at TO node_retired_at"
        )
        db.execute(
            "INSERT INTO threads(id,user_id,title,created_at,updated_at) "
            "VALUES('thread','owner','Thread','t','t')"
        )
        db.execute(
            """INSERT INTO artifact_identities
               (id,integration,type,external_id,created_at,updated_at)
               VALUES('artifact','github','pull-request','o/r#1','t','t')"""
        )
        db.execute(
            """INSERT INTO artifact_registrations
               (id,artifact_id,parent_thread_id,relationship_state,surfacing,
                node_retired_at,created_at,updated_at)
               VALUES('registration','artifact','thread','owner-active',
                      'settled','stamped-at','t','t')"""
        )
        db.execute("PRAGMA user_version=23")
        db.commit()
    return path


def test_fresh_schema_is_current_version():
    assert SCHEMA_VERSION == 146


def test_migration_renames_the_column_without_changing_the_value(tmp_path):
    path = _legacy_database(tmp_path)
    with closing(sqlite3.connect(path)) as db:
        result = migrate_to_current(db, 24)
        assert (result.migrated, result.from_version, result.to_version) == (True, 23, 24)
        columns = {row[1] for row in db.execute("PRAGMA table_info(artifact_registrations)")}
        assert "retired_at" in columns
        assert "node_retired_at" not in columns
        assert db.execute(
            "SELECT retired_at FROM artifact_registrations WHERE id='registration'"
        ).fetchone()[0] == "stamped-at"

        assert not migrate_to_current(db, 24).migrated
