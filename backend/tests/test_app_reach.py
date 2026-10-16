from __future__ import annotations

import json
import sqlite3

import pytest

from connections.app_lifecycle import (
    approve_proposal,
    create_app,
    create_pending_app,
    decline_proposal,
    propose_config,
    replace_reach,
)
from connections.app_reach import (
    AppNameOverlapError,
    AppProposalExpiredError,
    AppRevisionConflictError,
    app_reaches_thread,
    get_app,
    get_reach,
)
from infra.database import initialize_schema
from infra.migration_120_to_121_connected_apps import migrate_120_to_121
from tools.mcp_secrets import entry_name


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    for project_id in ("a", "b", "deleted"):
        db.execute(
            "INSERT INTO projects(id,name,created_at,updated_at) VALUES (?,?,?,?)",
            (project_id, project_id, "t", "t"),
        )
    thread_projects: tuple[tuple[str, str | None], ...] = (
        ("a-thread", "a"), ("b-thread", "b"), ("outside", None), ("gone", "deleted")
    )
    for thread_id, thread_project_id in thread_projects:
        db.execute(
            "INSERT INTO threads(id,user_id,project_id,created_at,updated_at) VALUES (?,?,?,?,?)",
            (thread_id, "person", thread_project_id, "t", "t"),
        )
    db.execute(
        "INSERT INTO threads(id,user_id,project_id,parent_thread_id,created_at,updated_at) VALUES (?,?,?,?,?,?)",
        ("b-child-of-a", "person", "b", "a-thread", "t", "t"),
    )
    db.commit()
    return db


def test_server_resolved_reach_fails_closed_for_missing_projects() -> None:
    db = _db()
    all_app = create_app("All", {}, {"mode": "all", "project_ids": [], "outside_projects": False}, db=db)
    selected = create_app(
        "Selected", {}, {"mode": "selected", "project_ids": ["a"], "outside_projects": True}, db=db
    )
    db.execute("DELETE FROM projects WHERE id='deleted'")
    db.commit()

    assert app_reaches_thread(all_app["id"], "a-thread", db=db)
    assert app_reaches_thread(all_app["id"], "outside", db=db)
    assert app_reaches_thread(selected["id"], "a-thread", db=db)
    assert app_reaches_thread(selected["id"], "outside", db=db)
    assert get_app(selected["id"], db=db)["config"] == {}
    assert not app_reaches_thread(selected["id"], "b-thread", db=db)
    assert not app_reaches_thread(selected["id"], "b-child-of-a", db=db)
    assert not app_reaches_thread(all_app["id"], "gone", db=db)
    assert not app_reaches_thread(selected["id"], "gone", db=db)


def test_name_overlap_is_checked_per_project_and_outside_scope() -> None:
    db = _db()
    create_app("GitHub", {}, {"mode": "selected", "project_ids": ["a"], "outside_projects": False}, db=db)
    create_app("GitHub", {}, {"mode": "selected", "project_ids": ["b"], "outside_projects": False}, db=db)

    with pytest.raises(AppNameOverlapError) as project_conflict:
        create_app("GitHub", {}, {"mode": "selected", "project_ids": ["a"], "outside_projects": False}, db=db)
    assert project_conflict.value.scope == "a"

    create_app("Calendar", {}, {"mode": "selected", "project_ids": [], "outside_projects": True}, db=db)
    with pytest.raises(AppNameOverlapError) as outside_conflict:
        create_app("Calendar", {}, {"mode": "all", "project_ids": [], "outside_projects": False}, db=db)
    assert outside_conflict.value.scope == "outside_projects"


def test_reach_replace_is_revision_checked_and_atomic() -> None:
    db = _db()
    app = create_app("GitHub", {}, {"mode": "selected", "project_ids": ["a"], "outside_projects": False}, db=db)

    with pytest.raises(AppRevisionConflictError):
        replace_reach(
            app["id"], {"mode": "selected", "project_ids": ["b"], "outside_projects": False},
            expected_revision=2, db=db,
        )
    assert get_reach(app["id"], db=db) == {"mode": "selected", "project_ids": ["a"], "outside_projects": False}

    db.execute(
        "CREATE TRIGGER fail_reach_insert BEFORE INSERT ON connected_app_reach "
        "WHEN NEW.project_id='b' BEGIN SELECT RAISE(ABORT, 'injected'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        replace_reach(
            app["id"], {"mode": "selected", "project_ids": ["b"], "outside_projects": False},
            expected_revision=1, db=db,
        )
    assert get_reach(app["id"], db=db) == {"mode": "selected", "project_ids": ["a"], "outside_projects": False}
    assert db.execute("SELECT revision FROM connected_apps WHERE id=?", (app["id"],)).fetchone()[0] == 1


def test_reach_change_notifies_only_threads_whose_app_availability_changes() -> None:
    db = _db()
    app = create_app("GitHub", {}, {"mode": "all", "project_ids": [], "outside_projects": False}, db=db)
    db.execute("DELETE FROM context_change_journal")
    db.commit()

    replace_reach(
        app["id"], {"mode": "selected", "project_ids": ["b"], "outside_projects": False},
        expected_revision=1, db=db,
    )

    notices = db.execute(
        "SELECT object_id,summary,details_json FROM context_change_journal ORDER BY object_id"
    ).fetchall()
    assert {row["object_id"] for row in notices} == {"a-thread", "b-child-of-a", "gone", "outside"}
    assert "b-thread" not in {row["object_id"] for row in notices}
    assert all(row["summary"] == "GitHub: availability changed" for row in notices)
    assert all(json.loads(row["details_json"])["path"] == f"connected_apps/{app['id']}" for row in notices)


def test_app_state_notices_collapse_to_the_latest_state_per_delivery() -> None:
    from connections.app_notices import append_app_notice
    from infra.passive_digest import build_digest

    db = _db()
    app = create_app(
        "GitHub", {}, {"mode": "selected", "project_ids": ["a"], "outside_projects": False}, db=db,
    )
    db.execute("DELETE FROM context_change_journal")
    db.commit()
    append_app_notice(app["id"], "GitHub: working", db=db, state="working")
    append_app_notice(app["id"], "GitHub: failed", db=db, state="failed")

    rows = db.execute(
        "SELECT * FROM context_change_journal WHERE object_id='a-thread' ORDER BY seq DESC"
    ).fetchall()
    _content, events = build_digest(rows, overflow=False, clock=None)

    assert len(events) == 1
    assert events[0]["summary"] == "GitHub: failed"
    assert events[0]["details"]["state"] == "failed"


def test_expired_proposals_are_read_as_expired_and_lazily_declined() -> None:
    db = _db()
    db.execute("UPDATE threads SET title='Planning' WHERE id='a-thread'")
    db.commit()
    reach = {"mode": "selected", "project_ids": ["a"], "outside_projects": False}
    proposal = create_pending_app("New", {"command": "new"}, reach, actor_thread_id="a-thread", db=db)
    db.execute("UPDATE connected_apps SET state_since='2000-01-01T00:00:00' WHERE id=?", (proposal["id"],))
    db.commit()

    assert get_app(proposal["id"], db=db)["state"] == "expired"
    with pytest.raises(
        AppProposalExpiredError,
        match="This request expired after seven days. Ask Planning to send it again.",
    ):
        approve_proposal(proposal["id"], db=db)
    assert db.execute("SELECT 1 FROM connected_apps WHERE id=?", (proposal["id"],)).fetchone() is None
    audit = db.execute(
        "SELECT actor_kind,action FROM connected_app_audit WHERE app_id=?", (proposal["id"],)
    ).fetchone()
    assert tuple(audit) == ("system", "expire")

    app = create_app("Existing", {"command": "old"}, reach, db=db)
    propose_config(app["id"], {"command": "new"}, actor_thread_id="a-thread", expected_revision=1, db=db)
    db.execute("UPDATE connected_apps SET state_since='2000-01-01T00:00:00' WHERE id=?", (app["id"],))
    db.commit()

    declined = decline_proposal(app["id"], db=db)
    assert declined["state"] == "disconnected"
    assert declined["config"] == {"command": "old"}
    audit = db.execute(
        "SELECT actor_kind,action FROM connected_app_audit WHERE app_id=? ORDER BY rowid DESC", (app["id"],)
    ).fetchone()
    assert tuple(audit) == ("system", "expire")


def test_migration_moves_name_config_and_key_reference_to_app_id() -> None:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE key_store_entries (
            name TEXT PRIMARY KEY, label TEXT, description TEXT, added_by_thread_id TEXT,
            added_by_name TEXT, created_at TEXT NOT NULL, set_at TEXT, value_ref TEXT
        );
        """
    )
    config = {"command": "example", "secret_env": ["TOKEN"]}
    db.execute(
        "INSERT INTO settings(key,value,updated_at) VALUES ('tools',?, 't')",
        (json.dumps({"mcpServers": {"GitHub": config}, "other": True}),),
    )
    old_entry = entry_name("GitHub", "secret_env", "TOKEN")
    db.execute(
        "INSERT INTO key_store_entries(name,created_at,set_at,value_ref) VALUES (?, 't', 't', 'opaque-ref')",
        (old_entry,),
    )

    migrate_120_to_121(db)

    app = db.execute("SELECT id,name,config_json,reach_mode,outside_projects FROM connected_apps").fetchone()
    assert app["name"] == "GitHub"
    assert json.loads(app["config_json"]) == config
    assert (app["reach_mode"], app["outside_projects"]) == ("all", 0)
    assert db.execute("SELECT project_id FROM connected_app_reach").fetchall() == []
    assert json.loads(db.execute("SELECT value FROM settings WHERE key='tools'").fetchone()[0]) == {"other": True}
    assert db.execute("SELECT 1 FROM key_store_entries WHERE name=?", (old_entry,)).fetchone() is None
    new_entry = entry_name(app["id"], "secret_env", "TOKEN")
    assert db.execute("SELECT value_ref FROM key_store_entries WHERE name=?", (new_entry,)).fetchone()[0] == "opaque-ref"
