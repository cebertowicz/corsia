"""A created conductor binds the model its caller states (#3288, #1645 residual)."""

from __future__ import annotations


import asyncio
import json

import pytest


def _thread_model(db, thread_id):
    return db.execute("SELECT model FROM threads WHERE id=?", (thread_id,)).fetchone()["model"]


def test_ensure_corsia_agent_binds_the_stated_model(isolated_db):
    from infra import accounts
    from infra.corsia_agent import ensure_corsia_agent
    from infra.database import get_db

    accounts.create_user("newcomer", "long-enough-password")
    db = get_db()
    try:
        db.execute("DELETE FROM settings WHERE key='corsia_agent_thread_id'")
        db.commit()
        thread_id = ensure_corsia_agent("newcomer", model="claude-code/sonnet", db=db)
        assert _thread_model(db, thread_id) == "claude-code/sonnet"
    finally:
        db.close()


def test_every_caller_must_state_the_model():
    from infra.corsia_agent import ensure_corsia_agent

    with pytest.raises(TypeError, match="model"):
        ensure_corsia_agent("newcomer")


def test_backfill_deliberately_binds_no_model(monkeypatch, isolated_db):
    import infra.conductor as conductor
    from infra import accounts
    from infra.database import get_db

    accounts.create_user("owner", "long-enough-password")
    db = get_db()
    try:
        db.execute("DELETE FROM settings WHERE key='corsia_agent_thread_id'")
        db.execute(
            "INSERT INTO projects (id,name,created_at,updated_at) "
            "VALUES ('p1','P1','t','t')"
        )
        db.commit()
    finally:
        db.close()
    calls = []
    real = conductor.ensure_project_conductor

    def _spy(scope, user_id, **kwargs):
        calls.append((scope, kwargs["model"]))
        return real(scope, user_id, **kwargs)

    real_agent = conductor.ensure_corsia_agent

    def _agent_spy(user_id, **kwargs):
        calls.append(("workspace", kwargs["model"]))
        return real_agent(user_id, **kwargs)

    monkeypatch.setattr(conductor, "ensure_project_conductor", _spy)
    monkeypatch.setattr(conductor, "ensure_corsia_agent", _agent_spy)

    conductor.backfill_coordination_scopes()

    assert calls == [("workspace", None), ("p1", None)]


def test_the_first_accounts_workspace_conductor_is_model_less(monkeypatch, isolated_db):
    from infra import accounts
    import infra.corsia_agent as corsia_agent
    from infra.database import get_db

    db = get_db()
    try:
        db.execute("DELETE FROM users")
        db.commit()
    finally:
        db.close()
    calls = []
    real = corsia_agent.ensure_corsia_agent

    def _spy(user_id, **kwargs):
        calls.append(("workspace", kwargs["model"]))
        return real(user_id, **kwargs)

    monkeypatch.setattr(corsia_agent, "ensure_corsia_agent", _spy)

    accounts.create_user("first", "long-enough-password")

    # The user picks its first model in first-run setup (slice 2).
    assert calls == [("workspace", None)]


def _status_rows(*rows):
    return lambda: [dict(row) for row in rows]


def test_human_conductor_model_prefers_the_workspace_conductors_ready_model(
    monkeypatch, isolated_db,
):
    import llm.delegation_model as delegation_model
    import llm.model_status as model_status
    from infra.conductor import human_conductor_model
    from infra.corsia_agent import ensure_corsia_agent
    from infra.database import get_db

    ready = {"claude-code/sonnet", "external@x/qwen"}
    monkeypatch.setattr(
        delegation_model, "eligible_delegation_model",
        lambda selector: (selector, None) if selector in ready else (None, "not ready"),
    )
    monkeypatch.setattr(model_status, "get_model_status_rows", _status_rows(
        {"id": "codex/limited", "status": "unavailable"},
        {"id": "external@x/qwen", "status": "ready"},
    ))
    from infra import accounts

    accounts.create_user("u", "long-enough-password")
    db = get_db()
    try:
        db.execute("DELETE FROM settings WHERE key='corsia_agent_thread_id'")
        db.commit()
        thread_id = ensure_corsia_agent("u", model="claude-code/sonnet", db=db)
        assert human_conductor_model(db) == "claude-code/sonnet"
        db.execute("UPDATE threads SET model='codex/limited' WHERE id=?", (thread_id,))
        db.commit()
        assert human_conductor_model(db) is None
    finally:
        db.close()


def test_human_conductor_model_does_not_choose_a_ready_model_without_selection(
    monkeypatch, isolated_db,
):
    import llm.delegation_model as delegation_model
    import llm.model_status as model_status
    from infra.conductor import human_conductor_model

    monkeypatch.setattr(
        delegation_model, "eligible_delegation_model",
        lambda selector: (selector, None) if selector == "external@x/qwen" else (None, "not ready"),
    )
    monkeypatch.setattr(model_status, "get_model_status_rows", _status_rows(
        {"id": "external@x/qwen", "status": "ready"},
    ))

    assert human_conductor_model() is None


def test_human_conductor_model_is_none_when_nothing_is_ready(monkeypatch, isolated_db):
    import llm.model_status as model_status
    from infra.conductor import human_conductor_model

    monkeypatch.setattr(model_status, "get_model_status_rows", _status_rows(
        {"id": "codex/limited", "status": "unavailable"},
    ))

    assert human_conductor_model() is None


@pytest.mark.asyncio
async def test_human_project_api_keeps_its_first_input_for_model_selection(
    monkeypatch, isolated_db, tmp_path,
):
    """An unavailable workspace selection never substitutes a catalog model."""
    import llm.delegation_model as delegation_model
    import llm.model_status as model_status
    from api import project_scaffolding, projects
    from auth_fixtures import OWNER_TOKEN, OWNER_USER
    from infra.conductor import get_project_conductor
    from infra.corsia_agent import ensure_corsia_agent
    from infra.database import get_db
    from infra.platform_paths import WorkspaceSelection
    from infra.send_service import send_thread_input
    from infra.send_types import InboxQueuedInput

    workspace = tmp_path / "workspace"
    monkeypatch.setattr(project_scaffolding.config, "WORKSPACE", workspace)
    monkeypatch.setattr(
        project_scaffolding.config,
        "WORKSPACE_SELECTION",
        WorkspaceSelection(workspace_id="w", root=workspace, name="W"),
    )
    monkeypatch.setattr(
        delegation_model, "eligible_delegation_model",
        lambda selector: (
            ("external@x/qwen", None) if selector == "external@x/qwen" else (None, "not ready")
        ),
    )
    monkeypatch.setattr(model_status, "get_model_status_rows", _status_rows(
        {"id": "external@x/qwen", "status": "ready"},
    ))

    db = get_db()
    try:
        db.execute("DELETE FROM settings WHERE key='corsia_agent_thread_id'")
        db.commit()
        workspace_conductor = ensure_corsia_agent(
            OWNER_USER, model="codex/unavailable", db=db,
        )
        assert workspace_conductor
    finally:
        db.close()

    created = await projects.create_project(
        OWNER_TOKEN, projects.ProjectCreate(name="Model-less project"),
    )
    project_id = created["project"]["id"]

    db = get_db()
    try:
        conductor_id = get_project_conductor(project_id, db=db)
        thread = db.execute("SELECT * FROM threads WHERE id=?", (conductor_id,)).fetchone()
        assert thread["model"] is None
    finally:
        db.close()

    result = await send_thread_input(
        conductor_id,
        thread,
        "first conductor input",
        user={"id": OWNER_USER},
        input_id="model-less-project-input",
        native_mode="delegation",
    )

    assert isinstance(result, InboxQueuedInput)
    db = get_db()
    try:
        row = db.execute(
            "SELECT target_model,delivery_state,write_outcome,delivery_error "
            "FROM messages WHERE id='model-less-project-input'"
        ).fetchone()
        assert tuple(row) == (None, "queued", "proved_no_write", "no_model_selected")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_the_human_project_route_binds_the_human_conductor_model(monkeypatch):
    from api import projects

    seen = {}

    def _initialize(db, **kwargs):
        seen.update(kwargs)
        return {"id": "p1"}

    class _Db:
        def execute(self, *_args):
            return self

        def fetchone(self):
            return None

        def close(self):
            pass

    async def _run_now(callback):
        return callback()

    monkeypatch.setattr(projects, "authenticate", lambda _token: {"id": "user-1"})
    monkeypatch.setattr(projects, "get_db", _Db)
    monkeypatch.setattr(projects, "run_in_db", _run_now)
    monkeypatch.setattr(projects, "initialize_project", _initialize)
    monkeypatch.setattr(projects, "_project_api_projection", lambda p, db: {"path": "x"})
    monkeypatch.setattr(
        "infra.conductor.human_conductor_model", lambda db=None: "claude-code/sonnet",
    )

    nudged = []
    monkeypatch.setattr(projects, "notify_projects_changed", nudged.append)

    await projects.create_project("token", projects.ProjectCreate(name="Demo"))

    assert seen["conductor_model"] == "claude-code/sonnet"
    assert nudged == ["user-1"]


def test_project_create_refuses_a_caller_model_that_is_not_ready(monkeypatch, isolated_db):
    import llm.delegation_model as delegation_model
    from infra import accounts
    from infra.database import get_db
    from tools.projects import CreateProjectTool

    asked = []

    def _eligible(selector):
        asked.append(selector)
        return None, (
            f"model {selector!r} resolves to 'codex/gpt' but is unavailable "
            "(usage_limited); choose a ready selector: claude-code/sonnet (usage {})."
        )

    monkeypatch.setattr(delegation_model, "eligible_delegation_model", _eligible)
    accounts.create_user("owner", "long-enough-password")
    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,title,model,created_at,updated_at)"
            " VALUES ('caller','owner','Caller','codex/gpt','t','t')"
        )
        db.commit()
    finally:
        db.close()

    result = asyncio.run(CreateProjectTool().execute(name="Blocked", _thread_id="caller"))

    assert asked == ["codex/gpt"]
    assert result.startswith("❌ The project's conductor runs on your current model")
    assert "choose a ready selector: claude-code/sonnet" in result
    db = get_db()
    try:
        assert db.execute("SELECT 1 FROM projects WHERE name='Blocked'").fetchone() is None
    finally:
        db.close()


def test_project_create_returns_the_bound_conductor(monkeypatch, isolated_db, tmp_path):
    import llm.delegation_model as delegation_model
    from api import project_scaffolding
    from infra import accounts
    from infra.conductor import get_scope_coordinator
    from infra.database import get_db
    from infra.platform_paths import WorkspaceSelection
    from tools.projects import CreateProjectTool

    workspace = tmp_path / "workspace"
    monkeypatch.setattr(project_scaffolding.config, "WORKSPACE", workspace)
    monkeypatch.setattr(
        project_scaffolding.config, "WORKSPACE_SELECTION",
        WorkspaceSelection(workspace_id="w", root=workspace, name="W"),
    )
    monkeypatch.setattr(project_scaffolding, "sync_bootload_documents", lambda *a, **k: None)
    # The catalog canonicalizes an alias; the conductor binds the canonical id.
    monkeypatch.setattr(
        delegation_model, "eligible_delegation_model",
        lambda selector: ("claude-code/sonnet", None) if selector == "sonnet" else (None, "x"),
    )
    accounts.create_user("owner", "long-enough-password")
    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,title,model,created_at,updated_at)"
            " VALUES ('caller','owner','Caller','sonnet','t','t')"
        )
        db.commit()
    finally:
        db.close()

    payload = json.loads(asyncio.run(
        CreateProjectTool().execute(name="Bound", _thread_id="caller")
    ))

    db = get_db()
    try:
        conductor_id = get_scope_coordinator(payload["project_id"], db=db)
        assert payload["conductor_thread_id"] == conductor_id
        assert payload["conductor_model"] == "claude-code/sonnet"
        assert _thread_model(db, conductor_id) == "claude-code/sonnet"
    finally:
        db.close()
