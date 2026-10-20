"""Schema v52 adds nullable workflow task fields without backfill."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION


_WORKFLOW_COLUMNS = {"workflow_ref", "workflow_version", "workflow_step", "difficulty"}
# Schema 52 added the version column as ``workflow_commit``; 56 renamed it.
_V52_COLUMNS = {"workflow_ref", "workflow_commit", "workflow_step", "difficulty"}


def _columns(db) -> set[str]:
    return {str(row[1]) for row in db.execute("PRAGMA table_info(plan_nodes)")}


def test_fresh_schema_has_nullable_workflow_fields():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        details = {
            str(row[1]): row for row in db.execute("PRAGMA table_info(plan_nodes)")
        }

    assert SCHEMA_VERSION == 131
    assert _WORKFLOW_COLUMNS <= set(details)
    assert all(details[name][3] == 0 for name in _WORKFLOW_COLUMNS)


def test_startup_migrates_v51_plan_nodes_without_backfill(tmp_path):
    path = tmp_path / "workflow-fields.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        for column in _WORKFLOW_COLUMNS:
            db.execute(f"ALTER TABLE plan_nodes DROP COLUMN {column}")
        db.execute("PRAGMA user_version=51")
        db.commit()

    with closing(sqlite3.connect(path)) as db:
        result = migrate_to_current(db, 52)

        assert (result.migrated, result.from_version, result.to_version) == (True, 51, 52)
        assert _V52_COLUMNS <= _columns(db)
        assert not migrate_to_current(db, 52).migrated
