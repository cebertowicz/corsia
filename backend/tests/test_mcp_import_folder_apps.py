"""A Claude Code app set up for a folder in no project comes over waiting for a place (decision 3)."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from connections.app_lifecycle import decline_proposal, replace_reach
from connections.app_reach import app_reaches_thread, get_app
from infra.database import initialize_schema
from tools import mcp_import

EMPTY = {"mode": "selected", "project_ids": [], "outside_projects": False}


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    for project_id in ("project-a", "project-b"):
        db.execute(
            "INSERT INTO projects(id,name,created_at,updated_at) VALUES (?,?,?,?)",
            (project_id, project_id, "t", "t"),
        )
    for thread_id, owner in (("thread-a", "project-a"), ("outside", None)):
        db.execute(
            "INSERT INTO threads(id,user_id,project_id,created_at,updated_at) VALUES (?,?,?,?,?)",
            (thread_id, "user", owner, "t", "t"),
        )
    db.commit()
    return db


def _claude_config(home: Path, folder: Path, apps: dict[str, dict]) -> None:
    (home / ".claude.json").write_text(
        json.dumps({"projects": {str(folder): {"mcpServers": apps}}}), encoding="utf-8",
    )


def _import(home: Path, db, name: str = "Postgres") -> dict:
    return mcp_import.import_selected(
        [{"source": "claude_code", "name": name}], db=db, home=home, actor="importer",
    )["rows"][0]


@pytest.fixture
def folder_app(tmp_path):
    db = _db()
    folder = tmp_path / "notes"
    folder.mkdir()
    _claude_config(tmp_path, folder, {"Postgres": {"command": "npx", "env": {"PG_TOKEN": "pg-secret"}}})
    row = _import(tmp_path, db)
    yield db, row, folder
    db.close()


@pytest.fixture
def api(monkeypatch, folder_app):
    from api import connected_apps, mcp
    from infra.auth import require_user

    db, row, _folder = folder_app
    actor = {"id": "user", "token_kind": "session"}
    app = FastAPI()
    app.include_router(mcp.router)
    app.dependency_overrides[require_user] = lambda: actor
    connects: list[str] = []

    async def immediate(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def runtime(action: str, app_id: str) -> dict[str, object]:
        connects.append(action)
        return {"state": "disconnected", "state_since": "t", "tool_count": 0, "failure_class": None}

    monkeypatch.setattr(connected_apps, "run_in_db", immediate)
    monkeypatch.setattr(connected_apps, "_db", lambda: db)
    monkeypatch.setattr(connected_apps, "_run_app_runtime", runtime)
    with TestClient(app) as client:
        yield client, db, row, actor, connects


def _state(db, app_id: str):
    app = get_app(app_id, db=db)
    return app["state"], app["revision"], app["reach"]


def test_outside_folder_app_comes_over_waiting_with_a_reach_that_reaches_nothing(folder_app):
    db, row, _folder = folder_app

    assert row["state"] == "awaiting_approval"
    assert row["folder"] == "notes"
    assert row["message"] == mcp_import.IMPORT_FOLDER_WAITING_FOR_PLACE.format(App=row["name"])
    assert row["message"].startswith(f"**{row['name']}**: ")
    assert "status" not in row
    app = get_app(row["id"], db=db)
    assert app["reach"] == EMPTY
    assert not app_reaches_thread(row["id"], "thread-a", db=db)
    assert not app_reaches_thread(row["id"], "outside", db=db)


def test_the_folder_is_display_only_and_never_stored_or_shown_in_full(folder_app, tmp_path):
    db, row, folder = folder_app
    app = get_app(row["id"], db=db)

    stored = json.dumps(app) + json.dumps(row)
    assert str(folder) not in stored and str(tmp_path) not in stored
    assert db.execute("SELECT COUNT(*) FROM connected_apps").fetchone()[0] == 1
    assert "cwd" not in app["config"] and "folder" not in app["config"]


def test_launch_cwd_of_an_imported_outside_folder_app_is_its_own_per_app_cache(folder_app, monkeypatch, tmp_path):
    from infra import config
    from tools import mcp_local_transport as transport

    db, row, folder = folder_app
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(config, "STATE_ROOT", state)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path / "home"))
    monkeypatch.setattr("tools.shell_guard.safehouse_version_error", lambda: None)
    monkeypatch.setattr("tools.shell_guard._safehouse_executable", lambda: tmp_path / "safehouse")
    monkeypatch.setattr("tools.safehouse_launch.mcp_private_state_profile", lambda *_args: "(version 1)")
    imported = get_app(row["id"], db=db)

    params, profile = transport.safehouse_stdio_parameters(
        imported["name"], {**imported["config"], "connected_app_id": row["id"]}, "npx", [], {},
    )
    profile.unlink(missing_ok=True)

    cache = tmp_path / "home" / "Library" / "Caches"
    assert Path(params.cwd).is_relative_to(cache) and row["id"] in str(params.cwd)
    assert str(folder) not in " ".join([str(params.cwd), *params.args, *params.env.values()])


def test_approve_refuses_an_empty_reach_and_changes_nothing(api):
    client, db, row, _actor, connects = api
    before = _state(db, row["id"])

    response = client.post(f"/api/mcp/servers/{row['id']}/approve", json={"expected_revision": before[1]})

    assert response.status_code == 422
    assert "Choose where it works" in response.json()["detail"]
    assert _state(db, row["id"]) == before and before[0] == "awaiting_approval"
    assert connects == []


def test_approve_refuses_a_reach_whose_only_project_was_deleted(api):
    client, db, row, _actor, connects = api
    db.execute("DELETE FROM connected_apps WHERE id=?", (row["id"],))
    db.commit()
    from connections.app_lifecycle import create_pending_app

    created = create_pending_app(
        "Stale", {"command": "x", "key_refs": []},
        {"mode": "selected", "project_ids": ["project-b"], "outside_projects": False},
        actor_thread_id="importer", db=db,
    )
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("DELETE FROM projects WHERE id='project-b'")
    db.commit()
    before = _state(db, created["id"])

    response = client.post(f"/api/mcp/servers/{created['id']}/approve", json={"expected_revision": before[1]})

    assert response.status_code == 422
    assert "Choose where it works" in response.json()["detail"]
    assert _state(db, created["id"]) == before
    assert connects == []


def test_approve_with_a_deleted_project_in_the_body_is_refused(api):
    client, db, row, _actor, connects = api
    before = _state(db, row["id"])

    response = client.post(
        f"/api/mcp/servers/{row['id']}/approve",
        json={"expected_revision": before[1],
              "reach": {"mode": "selected", "project_ids": ["gone"], "outside_projects": False}},
    )

    assert response.status_code == 422
    assert _state(db, row["id"]) == before and connects == []


def test_person_approve_writes_the_chosen_reach_and_the_approval_in_one_revision(api):
    client, db, row, _actor, connects = api
    revision = _state(db, row["id"])[1]
    reach = {"mode": "selected", "project_ids": ["project-a"], "outside_projects": False}

    response = client.post(
        f"/api/mcp/servers/{row['id']}/approve", json={"expected_revision": revision, "reach": reach},
    )

    assert response.status_code == 200
    assert _state(db, row["id"]) == ("disconnected", revision + 1, reach)
    assert connects == ["connect"]
    assert app_reaches_thread(row["id"], "thread-a", db=db)
    assert not app_reaches_thread(row["id"], "outside", db=db)


def test_person_approve_needs_a_current_revision(api):
    client, db, row, _actor, connects = api
    before = _state(db, row["id"])
    reach = {"mode": "all", "project_ids": [], "outside_projects": False}

    missing = client.post(f"/api/mcp/servers/{row['id']}/approve", json={"reach": reach})
    stale = client.post(
        f"/api/mcp/servers/{row['id']}/approve", json={"expected_revision": before[1] + 1, "reach": reach},
    )

    assert missing.status_code == 422
    assert stale.status_code == 409
    assert _state(db, row["id"]) == before and connects == []


def test_approve_without_a_reach_works_when_the_row_already_has_one(api):
    client, db, row, _actor, connects = api
    revision = _state(db, row["id"])[1]
    assert client.put(
        f"/api/mcp/servers/{row['id']}/reach",
        json={"expected_revision": revision, "reach": EMPTY},
    ).status_code == 409  # a pending row's reach is only set by approving it
    db.execute("UPDATE connected_apps SET reach_mode='all' WHERE id=?", (row["id"],))
    db.commit()

    response = client.post(f"/api/mcp/servers/{row['id']}/approve", json={"expected_revision": revision})

    assert response.status_code == 200 and connects == ["connect"]


def test_approve_checks_same_name_overlap_inside_the_transaction(api):
    client, db, row, _actor, connects = api
    from connections.app_lifecycle import create_app

    create_app(
        "Postgres", {"command": "other", "key_refs": []},
        {"mode": "selected", "project_ids": ["project-a"], "outside_projects": False}, db=db,
    )
    before = _state(db, row["id"])

    response = client.post(
        f"/api/mcp/servers/{row['id']}/approve",
        json={"expected_revision": before[1],
              "reach": {"mode": "selected", "project_ids": ["project-a"], "outside_projects": False}},
    )

    assert response.status_code == 422
    assert _state(db, row["id"]) == before and connects == []


@pytest.mark.parametrize("body", [None, {"expected_revision": 1},
                                  {"expected_revision": 1, "reach": {"mode": "all", "project_ids": [], "outside_projects": False}}])
def test_an_agent_cannot_approve_with_or_without_a_reach(api, body):
    client, db, row, actor, connects = api
    actor.clear()
    actor.update({"id": "user", "thread_id": "thread-a", "token_kind": "internal"})
    before = _state(db, row["id"])

    response = client.post(f"/api/mcp/servers/{row['id']}/approve", **({"json": body} if body else {}))

    assert response.status_code == 403
    assert _state(db, row["id"]) == before and connects == []
    assert db.execute(
        "SELECT actor_kind FROM connected_app_audit WHERE app_id=? AND action='refused_approve'", (row["id"],),
    ).fetchone()["actor_kind"] == "agent"


def test_an_agent_cannot_set_the_place_of_a_waiting_row(api):
    client, db, row, actor, _connects = api
    actor.clear()
    actor.update({"id": "user", "thread_id": "thread-a", "token_kind": "internal"})
    before = _state(db, row["id"])

    response = client.put(
        f"/api/mcp/servers/{row['id']}/reach",
        json={"expected_revision": before[1], "reach": {"mode": "all", "project_ids": [], "outside_projects": False}},
    )

    assert response.status_code == 403
    assert _state(db, row["id"]) == before


class _KeepOpen:
    """Share one in-memory database with code that closes the handle it gets."""

    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self) -> None:
        return None


def test_while_the_reach_is_empty_tool_calls_are_refused_before_any_key_lookup(folder_app, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from infra import key_store
    from tools.mcp import MCPToolWrapper
    from tools.tool_execution_context import mcp_reach_refusal

    db, row, _folder = folder_app
    lookups: list[object] = []
    for name in ("resolve_managed_values", "get_managed_value", "get_value"):
        monkeypatch.setattr(key_store, name, lambda *a, **k: lookups.append(a), raising=False)
    session = SimpleNamespace(call_tool=AsyncMock())
    wrapper = MCPToolWrapper(
        session, "Postgres",
        SimpleNamespace(name="query", description="q", inputSchema={"type": "object", "properties": {}}),
        app_id=row["id"],
    )
    monkeypatch.setattr("infra.database.get_db", lambda: _KeepOpen(db))

    for thread in ("thread-a", "outside"):
        refusal = asyncio.run(mcp_reach_refusal(wrapper, {"id": thread}))
        assert refusal and refusal.startswith("Error: This connected app is not available")
    assert lookups == [] and session.call_tool.await_count == 0


def test_declining_a_waiting_import_deletes_its_stored_keys(folder_app):
    db, row, _folder = folder_app
    assert _app_keys(db, row["id"])

    decline_proposal(row["id"], db=db)

    assert db.execute("SELECT 1 FROM connected_apps WHERE id=?", (row["id"],)).fetchone() is None
    assert _app_keys(db, row["id"]) == []


def test_an_expired_waiting_import_deletes_its_stored_keys(folder_app):
    db, row, _folder = folder_app
    assert _app_keys(db, row["id"])
    db.execute("UPDATE connected_apps SET state_since='2000-01-01T00:00:00' WHERE id=?", (row["id"],))
    db.commit()

    outcome = decline_proposal(row["id"], db=db)

    assert outcome == {"id": row["id"], "removed": True}
    assert _app_keys(db, row["id"]) == []


def _app_keys(db, app_id: str) -> list[str]:
    from tools.mcp_secrets import entry_name

    rows = db.execute("SELECT name FROM key_store_entries").fetchall()
    return [r["name"] for r in rows if r["name"] == entry_name(app_id, "secret_env", "PG_TOKEN")]


def test_replace_reach_still_refuses_a_waiting_row(folder_app):
    db, row, _folder = folder_app
    from connections.app_reach import AppPendingApprovalError

    with pytest.raises(AppPendingApprovalError):
        replace_reach(row["id"], EMPTY, expected_revision=1, db=db)


# --- the folder label: display only (Governance C3) ---

def test_the_label_is_stored_beside_the_config_and_never_in_the_runtime_config(folder_app):
    from connections.app_reach import runtime_app_config

    db, row, _folder = folder_app
    stored = json.loads(db.execute("SELECT config_json FROM connected_apps WHERE id=?", (row["id"],)).fetchone()[0])
    assert stored["source_folder"] == "notes"
    app = get_app(row["id"], db=db)
    assert app["source_folder"] == "notes" and "source_folder" not in app["config"]
    db.execute("UPDATE connected_apps SET state='disconnected' WHERE id=?", (row["id"],))
    _name, runtime = runtime_app_config(row["id"], db=db)
    assert "source_folder" not in runtime and "notes" not in json.dumps(runtime)


def test_only_the_display_surfaces_read_the_label():
    """A new reader of the label must be reviewed: nothing may use it for reach, launch or matching."""
    import re

    backend = Path(__file__).resolve().parent.parent
    readers = sorted(
        str(path.relative_to(backend))
        for path in backend.rglob("*.py")
        if "tests" not in path.parts and ".venv" not in path.parts and re.search(r"source_folder|SOURCE_FOLDER_KEY", path.read_text(encoding="utf-8"))
    )
    assert readers == [
        "api/connected_app_rows.py", "api/connected_apps.py", "connections/app_lifecycle.py",
        "connections/app_reach.py", "connections/app_source_folder.py", "tools/mcp_import.py",
    ]


def test_the_row_the_app_list_returns_carries_the_label_and_no_path(api, tmp_path):
    client, _db_, row, _actor, _connects = api
    servers = client.get("/api/mcp/servers").json()["servers"]
    assert servers[0]["folder"] == "notes"
    assert str(tmp_path) not in json.dumps(servers)


def test_an_agent_cannot_forge_the_label_inside_a_posted_config(api):
    client, db, _row, actor, _connects = api
    actor.clear()
    actor.update({"id": "user", "thread_id": "thread-a", "token_kind": "internal"})
    response = client.post("/api/mcp/servers", json={
        "name": "Forged", "config": {"command": "npx", "source_folder": "Your bank"}, "key_refs": [],
    })
    assert response.status_code in {400, 422}
    assert db.execute("SELECT COUNT(*) FROM connected_apps WHERE name='Forged'").fetchone()[0] == 0


@pytest.mark.parametrize("raw, expected", [
    ("shop", "shop"),
    ("  my  ‮shop\x00\n ", "my shop"),
    ("line break", "line break"),
    ("", None), ("   ", None), ("\x07\x1b", None), (None, None), (7, None),
    ("x" * 60, "x" * 60),
    ("x" * 61, "x" * 59 + "…"),
])
def test_the_sanitizer_keeps_plain_text_and_caps_the_length(raw, expected):
    from connections.app_source_folder import clean_folder_label

    assert clean_folder_label(raw) == expected


def test_a_hostile_folder_name_comes_over_as_a_short_plain_label(tmp_path):
    db = _db()
    folder = tmp_path / ("a‮b" + "z" * 120)
    folder.mkdir()
    _claude_config(tmp_path, folder, {"Postgres": {"command": "npx"}})
    row = _import(tmp_path, db)
    assert len(row["folder"]) == 60 and row["folder"].endswith("…")
    assert "‮" not in row["folder"] and get_app(row["id"], db=db)["source_folder"] == row["folder"]


@pytest.mark.parametrize("raw, expected", [
    ("/Users/me/work/shop", "shop"), ("/Users/me/work/shop/", "shop"), ("C:\\Users\\me\\shop", "shop"),
    ("a/b\\c", "c"), ("/", None), ("///", None), ("work / shop", "shop"),
])
def test_the_sanitizer_keeps_only_the_last_path_segment(raw, expected):
    from connections.app_source_folder import clean_folder_label

    assert clean_folder_label(raw) == expected


def test_a_full_path_given_to_create_pending_app_is_stored_and_returned_as_its_last_segment_only():
    from connections.app_lifecycle import create_pending_app

    db = _db()
    created = create_pending_app(
        "Postgres", {"command": "npx"}, EMPTY, actor_thread_id="thread-a",
        source_folder="/Users/me/private/clients/shop", db=db,
    )
    stored = db.execute("SELECT config_json FROM connected_apps WHERE id=?", (created["id"],)).fetchone()[0]
    assert json.loads(stored)["source_folder"] == "shop"
    assert get_app(created["id"], db=db)["source_folder"] == "shop"
    assert "private" not in stored and "/Users" not in stored


def test_the_app_list_has_one_fixed_order_whatever_the_ids_are():
    """Ids are random, so id order differs between installs and between runs; the list goes by name."""
    from connections.app_lifecycle import create_pending_app
    from connections.app_reach import list_apps

    db = _db()
    for app_id, name in (("a-1", "Postgres"), ("b-2", "Acme"), ("c-3", "linear"), ("d-4", "Notes"), ("e-5", "Acme")):
        create_pending_app(name, {"command": "npx"}, EMPTY, actor_thread_id="thread-a", app_id=app_id, db=db)
    listed = [(app["name"], app["id"]) for app in list_apps(db=db)]
    assert listed == [("Acme", "b-2"), ("Acme", "e-5"), ("linear", "c-3"), ("Notes", "d-4"), ("Postgres", "a-1")]
