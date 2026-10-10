"""Schema v32 re-derives every stored plan parent from owner identity (#2077)."""

from __future__ import annotations

from contextlib import closing
import sqlite3

import infra.config as config
from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.schema import SCHEMA_VERSION


def test_fresh_schema_is_current_version():
    assert SCHEMA_VERSION == 103


def _seed_v31(path) -> None:
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        for project_id in ("project-1", "project-2"):
            db.execute(
                "INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,'t','t')",
                (project_id, project_id),
            )
        for thread_id, project_id, spawned_by in (
            ("parent", "project-1", None),
            ("child", "project-1", "parent"),
            ("moved-child", "project-2", "parent"),
            ("inverted", "project-1", "child"),
            ("self-spawned", "project-1", "self-spawned"),
        ):
            db.execute(
                """INSERT INTO threads (id,user_id,project_id,spawned_by,created_at,updated_at)
                   VALUES (?,'user-1',?,?,'t','t')""",
                (thread_id, project_id, spawned_by),
            )
        for plan_id, owner_kind, owner_id, parent in (
            ("old-workspace", "workspace_scope", "retired-workspace", None),
            ("project-1", "project_scope", "project-1", "old-workspace"),
            ("project-2", "project_scope", "project-2", "old-workspace"),
            # The spawner's plan was stored below its own grandchild.
            ("inverted", "thread", "inverted", "project-1"),
            ("parent", "thread", "parent", "inverted"),
            ("child", "thread", "child", "project-1"),
            ("moved-child", "thread", "moved-child", "parent"),
            ("deleted-owner", "thread", "deleted-owner", "parent"),
            ("self-spawned", "thread", "self-spawned", "parent"),
        ):
            db.execute(
                """INSERT INTO plans (id,owner_kind,owner_id,parent_plan_id,created_at,updated_at)
                   VALUES (?,?,?,?,'t','t')""",
                (plan_id, owner_kind, owner_id, parent),
            )
        db.execute("PRAGMA user_version=31")
        db.commit()


def _parents(db) -> dict[str, str | None]:
    return dict(db.execute("SELECT id,parent_plan_id FROM plans"))


def test_startup_migration_re_derives_every_plan_parent(tmp_path):
    path = tmp_path / "v31-stale-plan-parents.db"
    _seed_v31(path)

    with closing(sqlite3.connect(path)) as db:
        result = migrate_to_current(db, 32)
        assert (result.migrated, result.from_version, result.to_version) == (True, 31, 32)
        workspace = db.execute(
            "SELECT id FROM plans WHERE owner_kind='workspace_scope' AND owner_id=?",
            (config.WORKSPACE_SELECTION.workspace_id,),
        ).fetchone()[0]

        assert _parents(db) == {
            "old-workspace": None,
            workspace: None,
            "project-1": workspace,
            "project-2": workspace,
            "parent": "project-1",
            "child": "parent",
            "inverted": "child",
            "moved-child": "project-2",
            "deleted-owner": "parent",
            "self-spawned": "project-1",
        }
        assert not migrate_to_current(db, 32).migrated


def test_a_second_run_changes_no_revision(tmp_path):
    path = tmp_path / "v31-rerun.db"
    _seed_v31(path)

    with closing(sqlite3.connect(path)) as db:
        migrate_to_current(db, 32)
        before = dict(db.execute("SELECT id,revision FROM plans"))
        MIGRATIONS[31](db)
        db.commit()
        assert dict(db.execute("SELECT id,revision FROM plans")) == before
