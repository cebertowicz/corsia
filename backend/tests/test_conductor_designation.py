from __future__ import annotations

import json
import sqlite3

import pytest


def _insert_session(db, thread_id: str) -> None:
    db.execute(
        "INSERT INTO threads "
        "(id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
        (thread_id, "steve", thread_id, "t", "t"),
    )


def _insert_project(db, project_id: str, name: str, path: str) -> None:
    db.execute(
        "INSERT INTO projects (id,name,path,created_at,updated_at) VALUES (?,?,?,?,?)",
        (project_id, name, path, "t", "t"),
    )


def test_ensure_scope_conductor_creates_workspace_thread_once(isolated_db):
    from infra import accounts
    from infra.conductor import WORKSPACE_SCOPE, ensure_scope_conductor, get_conductor
    from infra.database import get_db

    accounts.create_user("newcomer", "long-enough-password")
    db = get_db()
    try:
        db.execute("DELETE FROM settings WHERE key='workspace_conductor_session_id'")
        db.execute("DELETE FROM threads")
        db.commit()

        first = ensure_scope_conductor(WORKSPACE_SCOPE, "newcomer", model=None, db=db)
        second = ensure_scope_conductor(WORKSPACE_SCOPE, "newcomer", model=None, db=db)

        rows = db.execute(
            "SELECT id,user_id,title,model,project_id FROM threads WHERE title='Conductor'"
        ).fetchall()
        assert first == second == get_conductor(WORKSPACE_SCOPE, db=db)
        assert [dict(row) for row in rows] == [
            {
                "id": first,
                "user_id": "newcomer",
                "title": "Conductor",
                "model": None,
                "project_id": None,
            }
        ]
    finally:
        db.close()


def test_ensure_scope_conductor_designates_project_thread(isolated_db):
    from infra import accounts
    from infra.conductor import ensure_scope_conductor, get_conductor
    from infra.database import get_db

    accounts.create_user("newcomer", "long-enough-password")
    db = get_db()
    try:
        _insert_project(db, "project-a", "Project A", "/workspace/projects/a")
        db.commit()

        thread_id = ensure_scope_conductor("project-a", "newcomer", model=None, db=db)

        row = db.execute(
            "SELECT user_id,title,model,project_id FROM threads WHERE id=?",
            (thread_id,),
        ).fetchone()
        assert get_conductor("project-a", db=db) == thread_id
        assert dict(row) == {
            "user_id": "newcomer",
            "title": "Conductor",
            "model": None,
            "project_id": "project-a",
        }
    finally:
        db.close()


def test_backfill_scope_conductors_heals_missing_workspace_and_project(isolated_db):
    from infra import accounts
    from infra.conductor import WORKSPACE_SCOPE, backfill_scope_conductors, get_conductor
    from infra.database import get_db

    accounts.create_user("newcomer", "long-enough-password")
    db = get_db()
    try:
        db.execute("DELETE FROM settings WHERE key='workspace_conductor_session_id'")
        db.execute("DELETE FROM threads")
        _insert_project(db, "project-a", "Project A", "/workspace/projects/a")
        db.commit()

        healed = backfill_scope_conductors(db=db)

        assert [scope for scope, _thread_id in healed] == [WORKSPACE_SCOPE, "project-a"]
        assert get_conductor(WORKSPACE_SCOPE, db=db) is not None
        assert get_conductor("project-a", db=db) is not None
        assert db.execute("SELECT COUNT(*) FROM threads WHERE title='Conductor'").fetchone()[0] == 2
    finally:
        db.close()


@pytest.mark.parametrize("reader", ["designation", "scopes"])
@pytest.mark.parametrize(
    "stored_value",
    [
        pytest.param("{", id="malformed-json"),
        pytest.param(json.dumps("candidate"), id="non-object-json"),
        pytest.param(sqlite3.Binary(b"\xff"), id="invalid-utf8"),
        pytest.param(json.dumps({"session_id": "   "}), id="blank-session-id"),
    ],
)
def test_workspace_readers_reject_corrupt_designation(
    isolated_db,
    reader,
    stored_value,
):
    from infra.conductor import (
        ConductorSettingError,
        get_conductor,
        get_conductor_scopes,
    )
    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "INSERT INTO settings (key,value,updated_at) VALUES (?,?,?)",
            ("workspace_conductor_session_id", stored_value, "t"),
        )
        db.commit()

        with pytest.raises(ConductorSettingError, match="workspace conductor"):
            if reader == "designation":
                get_conductor("workspace", db=db)
            else:
                get_conductor_scopes("candidate", db=db)
    finally:
        db.close()


@pytest.mark.parametrize("scope", ["workspace", "project-a"])
def test_set_conductor_change_invalidates_old_and_new_epochs_only(
    isolated_db,
    scope,
):
    from infra.conductor import set_conductor
    from infra.database import get_db

    db = get_db()
    try:
        for thread_id in ("old", "new", "other"):
            _insert_session(db, thread_id)
        _insert_project(db, "project-a", "Project A", "/workspace/projects/a")
        if scope == "workspace":
            db.execute(
                "INSERT INTO settings (key,value,updated_at) VALUES (?,?,?)",
                (
                    "workspace_conductor_session_id",
                    json.dumps({"session_id": "old"}),
                    "t",
                ),
            )
        else:
            db.execute(
                "UPDATE projects SET conductor_thread_id='old' WHERE id='project-a'"
            )
        db.commit()

        assert set_conductor(scope, "new", db=db) is True
        epochs = dict(
            db.execute(
                "SELECT id,runtime_context_epoch FROM threads ORDER BY id"
            ).fetchall()
        )
        assert epochs == {"new": 1, "old": 1, "other": 0}

        assert set_conductor(scope, "new", db=db) is True
        unchanged = dict(
            db.execute(
                "SELECT id,runtime_context_epoch FROM threads ORDER BY id"
            ).fetchall()
        )
        assert unchanged == epochs

        assert set_conductor(scope, None, db=db) is True
        cleared = dict(
            db.execute(
                "SELECT id,runtime_context_epoch FROM threads ORDER BY id"
            ).fetchall()
        )
        assert cleared == {"new": 2, "old": 1, "other": 0}
    finally:
        db.close()


@pytest.mark.parametrize("scope", ["workspace", "project-a"])
def test_set_conductor_rolls_back_designation_when_epoch_invalidation_fails(
    isolated_db,
    scope,
):
    from infra.conductor import get_conductor, set_conductor
    from infra.database import get_db

    db = get_db()
    try:
        _insert_session(db, "old")
        _insert_session(db, "new")
        _insert_project(db, "project-a", "Project A", "/workspace/projects/a")
        if scope == "workspace":
            db.execute(
                "INSERT INTO settings (key,value,updated_at) VALUES (?,?,?)",
                (
                    "workspace_conductor_session_id",
                    json.dumps({"session_id": "old"}),
                    "t",
                ),
            )
        else:
            db.execute(
                "UPDATE projects SET conductor_thread_id='old' WHERE id='project-a'"
            )
        db.execute(
            "CREATE TRIGGER reject_new_epoch BEFORE UPDATE OF runtime_context_epoch "
            "ON threads WHEN NEW.id='new' BEGIN "
            "SELECT RAISE(ABORT, 'epoch invalidation failed'); END"
        )
        db.commit()

        with pytest.raises(sqlite3.IntegrityError, match="epoch invalidation failed"):
            set_conductor(scope, "new", db=db)

        assert get_conductor(scope, db=db) == "old"
        assert dict(
            db.execute(
                "SELECT id,runtime_context_epoch FROM threads ORDER BY id"
            ).fetchall()
        ) == {"new": 0, "old": 0}
    finally:
        db.close()
