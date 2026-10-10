from __future__ import annotations

import json
import sqlite3


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            spawned_by TEXT,
            agent_enabled INTEGER DEFAULT 0,
            agent_status TEXT DEFAULT 'active'
        );
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        );
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            path TEXT,
            conductor_thread_id TEXT
        );
        """
    )
    return conn


def _role_file(root, role: str, name: str, content: str) -> None:
    path = root / "context" / "roles" / role / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_infer_thread_roles_reads_agent_identity_from_thread() -> None:
    from core.role_context_sources import infer_thread_roles

    conn = _db()
    conn.execute("INSERT INTO threads (id, agent_enabled) VALUES ('agent', 1)")

    assert infer_thread_roles("agent", db=conn) == ("agent",)


def test_ordinary_thread_receives_no_role_context_and_directory_does_not_instantiate(tmp_path):
    from core.role_context_sources import resolve_role_context

    workspace = tmp_path / "workspace"
    _role_file(workspace, "agent", "AGENT.md", "agent instructions")
    _role_file(workspace, "conductor", "CONDUCTOR.md", "conductor instructions")

    conn = _db()
    conn.execute("INSERT INTO threads (id) VALUES ('ordinary')")
    before = dict(conn.execute("SELECT * FROM threads WHERE id='ordinary'").fetchone())

    resolution = resolve_role_context(
        thread_id="ordinary",
        project_id=None,
        workspace=workspace,
        db=conn,
    )

    after = dict(conn.execute("SELECT * FROM threads WHERE id='ordinary'").fetchone())
    assert resolution.roles == ()
    assert resolution.sources == ()
    assert resolution.conductor_scopes == ()
    assert after == before


def _select_workspace(monkeypatch, root) -> None:
    from infra.platform_paths import WorkspaceSelection

    monkeypatch.setattr(
        "infra.config.WORKSPACE_SELECTION",
        WorkspaceSelection(
            workspace_id="workspace-123", root=root.resolve(), name="Primary"
        ),
        raising=False,
    )


def test_agent_receives_workspace_then_project_agent_context_with_provenance(
    tmp_path, monkeypatch
):
    from core.role_context_sources import resolve_role_context

    _select_workspace(monkeypatch, tmp_path / "workspace")
    workspace = tmp_path / "workspace"
    project = workspace / "projects" / "demo"
    _role_file(workspace, "agent", "WORKSPACE.md", "workspace agent")
    _role_file(project, "agent", "PROJECT.md", "project agent")
    _role_file(workspace, "conductor", "NOPE.md", "conductor only")
    _role_file(project, "agent", "_ORDINARY.md", "underscore still inherits")

    conn = _db()
    conn.execute(
        "INSERT INTO threads (id, project_id, agent_enabled) VALUES (?, ?, 1)",
        ("agent", "project-a"),
    )
    conn.execute(
        "INSERT INTO projects (id, name, path) VALUES (?, ?, ?)",
        ("project-a", "Demo", "workspace:/" "/workspace-123/projects/demo"),
    )

    resolution = resolve_role_context(
        thread_id="agent",
        project_id="project-a",
        workspace=workspace,
        db=conn,
    )

    assert resolution.roles == ("agent",)
    assert [source.metadata["scope"] for source in resolution.sources] == [
        "workspace",
        "project",
        "project",
    ]
    assert [source.metadata["relative_path"] for source in resolution.sources] == [
        "context/roles/agent/WORKSPACE.md",
        "context/roles/agent/_ORDINARY.md",
        "context/roles/agent/PROJECT.md",
    ]
    assert all(source.metadata["role"] == "agent" for source in resolution.sources)
    assert all(source.origin_section == "role" for source in resolution.sources)
    assert "underscore still inherits" in (resolution.sources[1].prompt_content or "")
    assert not any("conductor only" in (source.prompt_content or "") for source in resolution.sources)


def test_conductor_composes_agent_then_conductor_across_existing_scopes(
    tmp_path, monkeypatch
):
    from core.role_context_sources import resolve_role_context

    _select_workspace(monkeypatch, tmp_path / "workspace")
    workspace = tmp_path / "workspace"
    project = workspace / "projects" / "demo"
    _role_file(workspace, "agent", "A.md", "workspace agent")
    _role_file(project, "agent", "A.md", "project agent")
    _role_file(workspace, "conductor", "C.md", "workspace conductor")
    _role_file(project, "conductor", "C.md", "project conductor")

    conn = _db()
    conn.execute(
        "INSERT INTO threads (id, project_id, agent_enabled) VALUES (?, ?, 0)",
        ("conductor", "project-a"),
    )
    conn.execute(
        "INSERT INTO projects (id, name, path, conductor_thread_id) VALUES (?, ?, ?, ?)",
        (
            "project-a",
            "Demo",
            "workspace:/" "/workspace-123/projects/demo",
            "conductor",
        ),
    )
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
        (
            "workspace_conductor_session_id",
            json.dumps({"session_id": "conductor"}),
            "t",
        ),
    )

    resolution = resolve_role_context(
        thread_id="conductor",
        project_id="project-a",
        workspace=workspace,
        db=conn,
    )

    assert resolution.roles == ("agent", "conductor")
    assert [source.metadata["role"] for source in resolution.sources] == [
        "agent",
        "agent",
        "conductor",
        "conductor",
    ]
    assert [source.metadata["scope"] for source in resolution.sources] == [
        "workspace",
        "project",
        "workspace",
        "project",
    ]
    assert [scope["scope_type"] for scope in resolution.conductor_scopes] == [
        "workspace",
        "project",
    ]


def test_removing_role_file_removes_it_from_next_resolution(tmp_path):
    from core.role_context_sources import resolve_role_context

    workspace = tmp_path / "workspace"
    role_file = workspace / "context" / "roles" / "agent" / "AGENT.md"
    _role_file(workspace, "agent", "AGENT.md", "temporary role context")

    conn = _db()
    conn.execute("INSERT INTO threads (id, agent_enabled) VALUES ('agent', 1)")

    first = resolve_role_context(
        thread_id="agent",
        project_id=None,
        workspace=workspace,
        db=conn,
    )
    role_file.unlink()
    second = resolve_role_context(
        thread_id="agent",
        project_id=None,
        workspace=workspace,
        db=conn,
    )

    assert len(first.sources) == 1
    assert second.sources == ()


def test_temporary_subagent_does_not_inherit_parent_agent_role(tmp_path):
    from core.role_context_sources import resolve_role_context

    workspace = tmp_path / "workspace"
    _role_file(workspace, "agent", "AGENT.md", "persistent Agents only")
    conn = _db()
    conn.execute(
        "INSERT INTO threads (id, spawned_by, agent_enabled) VALUES (?, ?, 0)",
        ("subagent", "parent-agent"),
    )

    resolution = resolve_role_context(
        thread_id="subagent",
        project_id=None,
        workspace=workspace,
        db=conn,
    )

    assert resolution.roles == ()
    assert resolution.sources == ()


def test_missing_agent_identity_schema_is_visible_without_inventing_a_role(tmp_path):
    from core.role_context_sources import resolve_role_context

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE threads (id TEXT PRIMARY KEY);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            path TEXT,
            conductor_thread_id TEXT
        );
        """
    )
    conn.execute("INSERT INTO threads VALUES ('unknown')")

    resolution = resolve_role_context(
        thread_id="unknown",
        project_id=None,
        workspace=tmp_path,
        db=conn,
        strict_db=True,
    )

    assert resolution.roles == ()
    assert len(resolution.sources) == 1
    error = resolution.sources[0]
    assert error.inclusion == "omitted"
    assert error.freshness == "error"
    assert error.metadata["role"] is None
    assert error.metadata["repair_owner"] == "agents"
    assert "agent_enabled" in error.reason


def test_strict_project_scope_lookup_error_is_visible_and_keeps_workspace_role(
    tmp_path,
    monkeypatch,
):
    from core.role_context_sources import resolve_role_context

    workspace = tmp_path / "workspace"
    _role_file(workspace, "agent", "AGENT.md", "workspace agent context")
    conn = _db()
    conn.execute(
        "INSERT INTO threads (id, project_id, agent_enabled) VALUES (?, ?, 1)",
        ("agent", "project-a"),
    )

    def fail_project_lookup(*_args, **_kwargs):
        raise sqlite3.OperationalError("projects path schema unavailable")

    monkeypatch.setattr(
        "core.project_context.resolve_project_paths",
        fail_project_lookup,
    )

    resolution = resolve_role_context(
        thread_id="agent",
        project_id="project-a",
        workspace=workspace,
        db=conn,
        strict_db=True,
    )

    assert resolution.roles == ("agent",)
    included = [source for source in resolution.sources if source.inclusion == "included"]
    errors = [source for source in resolution.sources if source.inclusion == "omitted"]
    assert [source.metadata["scope"] for source in included] == ["workspace"]
    assert "workspace agent context" in (included[0].prompt_content or "")
    assert len(errors) == 1
    assert errors[0].freshness == "error"
    assert "projects path schema unavailable" in errors[0].reason


def test_database_less_resolution_does_not_invent_or_error_a_role(tmp_path):
    from core.role_context_sources import resolve_role_context

    resolution = resolve_role_context(
        thread_id="offline-reconstruction",
        project_id=None,
        workspace=tmp_path,
        db=None,
        strict_db=True,
    )

    assert resolution.roles == ()
    assert resolution.sources == ()


def test_symlinked_role_directory_is_reported_and_not_followed(tmp_path):
    from core.role_context_sources import resolve_role_context

    workspace = tmp_path / "workspace"
    external = tmp_path / "external-agent-role"
    external.mkdir()
    (external / "AGENT.md").write_text("must not load", encoding="utf-8")
    roles_root = workspace / "context" / "roles"
    roles_root.mkdir(parents=True)
    (roles_root / "agent").symlink_to(external, target_is_directory=True)
    conn = _db()
    conn.execute("INSERT INTO threads (id, agent_enabled) VALUES ('agent', 1)")

    resolution = resolve_role_context(
        thread_id="agent",
        project_id=None,
        workspace=workspace,
        db=conn,
    )

    assert resolution.roles == ("agent",)
    assert len(resolution.sources) == 1
    error = resolution.sources[0]
    assert error.inclusion == "omitted"
    assert error.freshness == "error"
    assert error.metadata["role"] == "agent"
    assert "cannot be a symlink" in error.reason
    assert error.prompt_content is None


def test_invalid_utf8_role_file_is_reported_without_aborting_resolution(tmp_path):
    from core.role_context_sources import resolve_role_context

    workspace = tmp_path / "workspace"
    role_file = workspace / "context" / "roles" / "agent" / "BROKEN.md"
    role_file.parent.mkdir(parents=True)
    role_file.write_bytes(b"\xff")
    conn = _db()
    conn.execute("INSERT INTO threads (id, agent_enabled) VALUES ('agent', 1)")

    resolution = resolve_role_context(
        thread_id="agent",
        project_id=None,
        workspace=workspace,
        db=conn,
    )

    assert resolution.roles == ("agent",)
    assert len(resolution.sources) == 1
    error = resolution.sources[0]
    assert error.inclusion == "omitted"
    assert error.freshness == "error"
    assert error.stable_reference == str(role_file)
    assert error.metadata["relative_path"] == "context/roles/agent/BROKEN.md"
    assert "failed to read role context" in error.reason
