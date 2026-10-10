"""Schema v41 designates conductors for scopes that predate them (#1940)."""

from __future__ import annotations

from contextlib import closing
import json
import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION
from tests.schema_rewind import rewind_below_v42, rewind_to_v88_policy


def _seed_user(db, user_id: str, created_at: str) -> None:
    db.execute(
        "INSERT INTO users (id, password_hash, created_at, password_changed_at) "
        "VALUES (?, 'x', ?, ?)",
        (user_id, created_at, created_at),
    )


def _seed_thread(
    db, thread_id: str, user_id: str, project_id: str | None, created_at: str
) -> None:
    db.execute(
        "INSERT INTO threads (id, user_id, title, project_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (thread_id, user_id, thread_id, project_id, created_at, created_at),
    )


def _seed_project(db, project_id: str, name: str) -> None:
    db.execute(
        "INSERT INTO projects (id, name, path, created_at, updated_at) "
        "VALUES (?, ?, ?, 't', 't')",
        (project_id, name, f"/workspace/projects/{project_id}"),
    )


def _legacy_v39_db(path):
    """A v39 install: two accounts, a member-owned project, an empty one."""
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        db.execute("DELETE FROM settings WHERE key='workspace_conductor_session_id'")
        _seed_user(db, "first", "2026-01-01T00:00:00")
        _seed_user(db, "member", "2026-02-01T00:00:00")
        _seed_project(db, "project-owned", "Owned")
        _seed_project(db, "project-empty", "Empty")
        _seed_thread(db, "member-thread", "member", "project-owned", "2026-03-01")
        _seed_thread(db, "ghost-thread", "ghost", "project-owned", "2026-02-01")
        rewind_below_v42(db)
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("PRAGMA user_version=39")
        db.commit()
    return path


def test_startup_migration_heals_scopes_with_membership_safe_owner(tmp_path):
    path = _legacy_v39_db(tmp_path / "legacy-conductor-v39.db")

    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (
            True,
            39,
            SCHEMA_VERSION,
        )

        workspace = json.loads(
            db.execute(
                "SELECT value FROM settings WHERE key='workspace_conductor_session_id'"
            ).fetchone()["value"]
        )["session_id"]
        assert dict(
            db.execute(
                "SELECT user_id, title, project_id FROM threads WHERE id=?",
                (workspace,),
            ).fetchone()
        ) == dict(user_id="first", title="Conductor", project_id=None)

        # The project conductor goes to the earliest ACCOUNT-BACKED thread
        # owner ('member'), not the ghost owner and not the first account:
        # healing must not grant the first account project membership.
        conductor = db.execute(
            "SELECT conductor_thread_id FROM projects WHERE id='project-owned'"
        ).fetchone()["conductor_thread_id"]
        row = db.execute(
            "SELECT user_id, title, project_id FROM threads WHERE id=?",
            (conductor,),
        ).fetchone()
        assert dict(row) == dict(
            user_id="member", title="Conductor", project_id="project-owned"
        )
        from infra.queries import user_in_project

        assert user_in_project("project-owned", "member", db=db) is True
        assert user_in_project("project-owned", "first", db=db) is False

        # A project with no member to prefer falls back to the first account.
        empty_conductor = db.execute(
            "SELECT conductor_thread_id FROM projects WHERE id='project-empty'"
        ).fetchone()["conductor_thread_id"]
        owner = db.execute(
            "SELECT user_id FROM threads WHERE id=?", (empty_conductor,)
        ).fetchone()["user_id"]
        assert owner == "first"

        assert not migrate_to_current(db, SCHEMA_VERSION).migrated


def test_startup_migration_preserves_deliberate_clear(tmp_path):
    """PUT /api/conductor with thread_id null must survive later startups."""
    from infra.conductor import get_conductor, set_conductor

    path = _legacy_v39_db(tmp_path / "legacy-conductor-clear-v39.db")
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        migrate_to_current(db, SCHEMA_VERSION)
        before = db.execute("SELECT COUNT(*) FROM threads").fetchone()[0]

        assert set_conductor("workspace", None, db=db) is True
        assert set_conductor("project-owned", None, db=db) is True

        # The next startup (and every one after) runs no reconciler: the
        # cleared designations stay cleared and no replacement thread appears.
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
        assert get_conductor("workspace", db=db) is None
        assert get_conductor("project-owned", db=db) is None
        assert db.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == before


def test_startup_migration_without_accounts_is_a_no_op(tmp_path):
    path = tmp_path / "legacy-conductor-empty-v39.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        db.execute("DELETE FROM settings WHERE key='workspace_conductor_session_id'")
        rewind_below_v42(db)
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("PRAGMA user_version=39")
        db.commit()

    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        migrate_to_current(db, SCHEMA_VERSION)
        assert db.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM projects WHERE conductor_thread_id IS NOT NULL"
        ).fetchone()[0] == 0
