"""Schema v56 renames plan_nodes.workflow_commit to workflow_version."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.schema import SCHEMA_VERSION
from tests.plan_writer_authorizer import raw_plan_seed


def _columns(db) -> set[str]:
    return {str(row[1]) for row in db.execute("PRAGMA table_info(plan_nodes)")}


def test_fresh_schema_names_the_workflow_version_column():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        columns = _columns(db)

    assert SCHEMA_VERSION == 139
    assert "workflow_version" in columns and "workflow_commit" not in columns


def test_startup_renames_v55_workflow_commit(tmp_path):
    path = tmp_path / "workflow-version.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        db.execute("ALTER TABLE plan_nodes RENAME COLUMN workflow_version TO workflow_commit")
        db.execute("PRAGMA user_version=55")
        db.commit()

    with closing(sqlite3.connect(path)) as db:
        result = migrate_to_current(db, 56)

        assert (result.migrated, result.from_version, result.to_version) == (True, 55, 56)
        assert "workflow_version" in _columns(db) and "workflow_commit" not in _columns(db)
        assert not migrate_to_current(db, 56).migrated


def test_rename_keeps_stored_versions_and_is_idempotent():
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("CREATE TABLE plan_nodes (id TEXT PRIMARY KEY, workflow_commit TEXT)")
        with raw_plan_seed(db):
            db.execute("INSERT INTO plan_nodes VALUES ('n1', 'abc123-dirty')")

        MIGRATIONS[55](db)
        MIGRATIONS[55](db)

        assert db.execute("SELECT workflow_version FROM plan_nodes").fetchone() == (
            "abc123-dirty",
        )
