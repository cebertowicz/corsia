from __future__ import annotations

import sqlite3

from tests.project_ref_helpers import project_ref, select_workspace


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            title TEXT,
            project_id TEXT,
            spawned_by TEXT,
            parent_thread_id TEXT,
            agent_enabled INTEGER DEFAULT 0
        );
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            path TEXT,
            conductor_thread_id TEXT
        );
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        );
        """
    )
    return conn


def _minimal_db_without_projects() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            title TEXT,
            project_id TEXT,
            spawned_by TEXT,
            parent_thread_id TEXT,
            agent_enabled INTEGER DEFAULT 0
        );
        """
    )
    return conn


def _skill(root, rel: str, text: str) -> None:
    path = root / rel / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _valid(name: str, description: str = "Use when this behavior applies.") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n\nDo the work.\n"


def test_unclosed_frontmatter_with_heading_body_fails_validation():
    from core.skill_validation import validate_skill_text

    result = validate_skill_text(
        "---\nname: broken\ndescription: Never closed.\n# Heading\n\nBody text.\n"
    )

    assert not result.ok
    assert any("missing closing frontmatter marker" in e for e in result.errors)


def test_validates_standard_skill_and_corsia_metadata():
    from core.skill_validation import validate_skill_text

    result = validate_skill_text(
        """---
name: code-review
description: Use when reviewing code for defects.
corsia:
  handler:
    kind: cli
    command: scripts/corsia review-pr
  model_profile: coding-high
---
# Code review
"""
    )

    assert result.ok
    assert result.name == "code-review"
    assert result.description == "Use when reviewing code for defects."
    assert result.metadata["corsia"]["handler"]["kind"] == "cli"


def test_rejects_missing_description_and_invalid_corsia_handler():
    from core.skill_validation import validate_skill_text

    result = validate_skill_text(
        """---
name: broken
corsia:
  handler:
    kind: quantum
---
# Broken
"""
    )

    assert not result.ok
    assert "description is required" in result.errors
    assert "corsia.handler.kind must be one of connection_action, cli, instruction, native_tool, script" in result.errors


def test_skill_template_escapes_frontmatter_description():
    from core.skill_validation import skill_template, validate_skill_text

    text = skill_template(
        "frontmatter-safe",
        "Use safely.\ncorsia:\n  handler:\n    kind: cli",
    )
    validation = validate_skill_text(text)

    assert validation.ok
    assert validation.name == "frontmatter-safe"
    assert validation.description == "Use safely.\ncorsia:\n  handler:\n    kind: cli"
    assert "corsia" not in validation.metadata


def test_resolves_workspace_project_full_ancestry_and_current_thread_overrides(tmp_path, monkeypatch):
    from core.skills_registry import resolve_effective_skills

    workspace = tmp_path / "workspace"
    project = workspace / "projects" / "corsia"
    workspace.mkdir()
    project.mkdir(parents=True)
    select_workspace(monkeypatch, workspace)

    _skill(workspace, "skills/review", _valid("review", "global review"))
    _skill(project, "skills/review", _valid("review", "project review"))
    _skill(project, ".threads/parent/skills/review", _valid("review", "parent review"))
    _skill(project, ".threads/current/skills/review", _valid("review", "current review"))
    _skill(project, ".threads/grand/skills/deploy", _valid("deploy", "grand deploy"))

    conn = _db()
    conn.execute(
        "INSERT INTO projects (id, name, path) VALUES (?, ?, ?)",
        ("proj-1", "CORSIA", project_ref("corsia")),
    )
    conn.execute("INSERT INTO threads (id, project_id) VALUES (?, ?)", ("grand", "proj-1"))
    conn.execute(
        "INSERT INTO threads (id, project_id, spawned_by) VALUES (?, ?, ?)",
        ("parent", "proj-1", "grand"),
    )
    conn.execute(
        "INSERT INTO threads (id, project_id, spawned_by) VALUES (?, ?, ?)",
        ("current", "proj-1", "parent"),
    )

    resolved = resolve_effective_skills(
        thread_id="current",
        project_id="proj-1",
        workspace=workspace,
        db=conn,
    )

    included = {row.name: row for row in resolved.included}
    assert included["review"].description == "current review"
    assert included["review"].scope == "thread"
    assert included["review"].scope_id == "current"
    assert included["deploy"].description == "grand deploy"

    omitted = {(row.name, row.scope_id) for row in resolved.omitted}
    assert ("review", "workspace") in omitted
    assert ("review", "proj-1") in omitted
    assert ("review", "parent") in omitted


def test_thread_skills_load_from_hidden_thread_dir_not_working(tmp_path, monkeypatch):
    """Thread skills live at ``.threads/{id}/skills`` — the legacy
    ``working/{id}/skills`` location is no longer scanned."""
    from core.skills_registry import resolve_effective_skills, skill_scope_root

    workspace = tmp_path / "workspace"
    project = workspace / "projects" / "corsia"
    project.mkdir(parents=True)
    select_workspace(monkeypatch, workspace)

    # New location loads; old location is invisible.
    _skill(project, ".threads/sess/skills/deploy", _valid("deploy", "new-layout deploy"))
    _skill(project, "working/sess/skills/stale", _valid("stale", "old-layout stale"))

    conn = _db()
    conn.execute(
        "INSERT INTO projects (id, name, path) VALUES (?, ?, ?)",
        ("proj-1", "CORSIA", project_ref("corsia")),
    )
    conn.execute("INSERT INTO threads (id, project_id) VALUES (?, ?)", ("sess", "proj-1"))

    resolved = resolve_effective_skills(
        thread_id="sess", project_id="proj-1", workspace=workspace, db=conn
    )
    names = {row.name for row in resolved.included}
    assert "deploy" in names
    assert "stale" not in names

    root = skill_scope_root(
        scope="thread", workspace=workspace, db=conn, project_id="proj-1", thread_id="sess"
    )
    assert root == project / ".threads" / "sess"


def test_invalid_and_duplicate_same_scope_skills_are_omitted(tmp_path):
    from core.skills_registry import resolve_effective_skills

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _skill(workspace, "skills/a", _valid("same", "first"))
    _skill(workspace, "skills/b", _valid("same", "second"))
    _skill(workspace, "skills/broken", "---\nname: broken\n---\n# Broken\n")

    conn = _db()
    resolved = resolve_effective_skills(
        thread_id="sess",
        project_id=None,
        workspace=workspace,
        db=conn,
    )

    assert not [row for row in resolved.included if row.scope != "system"]
    errors = {row.path.parent.name: row.errors for row in resolved.errors}
    assert "description is required" in errors["broken"]
    duplicate_errors = [row for row in resolved.errors if "duplicate skill name in workspace scope: same" in row.errors]
    assert len(duplicate_errors) == 2


def test_missing_projects_table_keeps_workspace_skills_available(tmp_path):
    from core.skills_registry import resolve_effective_skills

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _skill(workspace, "skills/review", _valid("review", "global review"))
    _skill(workspace, "threads/sess/skills/leaked", _valid("leaked", "legacy workspace thread"))

    conn = _minimal_db_without_projects()
    conn.execute("INSERT INTO threads (id, project_id) VALUES (?, ?)", ("sess", "proj-a"))

    resolved = resolve_effective_skills(
        thread_id="sess",
        project_id="proj-a",
        workspace=workspace,
        db=conn,
    )

    assert [row.name for row in resolved.included if row.scope != "system"] == [
        "review"
    ]


def test_context_sources_include_index_and_omitted_rows(tmp_path):
    from core.skills_registry import skill_context_sources

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _skill(workspace, "skills/review", _valid("review", "Use when reviewing code."))
    _skill(workspace, "skills/broken", "---\nname: broken\n---\n# Broken\n")

    conn = _db()
    sources = skill_context_sources(
        thread_id="sess",
        project_id=None,
        workspace=workspace,
        db=conn,
    )

    index = next(source for source in sources if source.source_id == "dynamic:skills-index")
    assert index.origin_section == "dynamic"
    assert index.label == "Skills"
    assert index.inclusion == "included"
    assert (
        "Before answering, compare the user request to the skill descriptions below."
        in (index.prompt_content or "")
    )
    assert (
        "If the user names a skill or the request matches a skill description, "
        "you MUST load that skill's SKILL.md before responding."
        in (index.prompt_content or "")
    )
    assert "Load the listed SKILL.md path with your file-read tool" in (
        index.prompt_content or ""
    )
    assert "generic CORSIA tool" in (index.prompt_content or "")
    assert "name=read_file" in (index.prompt_content or "")
    assert "review: Use when reviewing code." in (index.prompt_content or "")
    assert "Do the work" not in (index.prompt_content or "")

    broken = next(source for source in sources if source.source_id.startswith("skill-error:"))
    assert broken.source_id == "skill-error:workspace:workspace:broken"
    assert broken.inclusion == "omitted"
    assert broken.freshness == "error"
    assert broken.owner_surface == "document-viewer"
    assert broken.metadata["repair_owner"] == "skills"


def test_invalid_workflow_is_visible_through_existing_skill_diagnostics(tmp_path):
    from core.skills_registry import skill_context_sources

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _skill(
        workspace,
        "skills/workflow-broken",
        """---
name: workflow-broken
description: Broken workflow metadata.
corsia:
  workflow:
    steps:
      - id: build
        unexpected: value
---
# Broken workflow
""",
    )

    sources = skill_context_sources(
        thread_id="sess",
        project_id=None,
        workspace=workspace,
        db=_db(),
    )

    broken = next(source for source in sources if source.source_id.startswith("skill-error:"))
    assert broken.label == "Skill error: workflow-broken"
    assert "build" in broken.metadata["errors"][0]
    assert "unknown key" in broken.metadata["errors"][0]


def test_dynamic_context_registry_collects_skills_index(tmp_path):
    from core.dynamic_context_registry import collect_context_sources

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _skill(workspace, "skills/review", _valid("review", "Use when reviewing code."))

    conn = _db()
    conn.execute("INSERT INTO threads (id, project_id) VALUES (?, ?)", ("sess", None))

    collection = collect_context_sources(
        thread_id="sess",
        project_id=None,
        workspace=workspace,
        db=conn,
    )

    sources = {source.source_id: source for source in collection.sources}
    skills = sources["dynamic:skills-index"]
    assert skills.inclusion == "included"
    assert skills.metadata["provider"] == "skills"
    assert "review: Use when reviewing code." in (skills.prompt_content or "")


def test_project_dir_uses_authoritative_path_basename_not_reslugified_name(tmp_path, monkeypatch):
    """_project_dir composes the planning dir UNDER the given workspace using the
    authoritative basename from projects.path — NOT a fresh slug of the name that
    can diverge from the actual on-disk dir (EAS2). A renamed project whose dir
    basename ('custom-dir') differs from slug('My Project')=='my-project' is still
    resolved to its real dir, so the migration/reader never silently skip it."""
    from core.skills_registry import _project_dir

    workspace = tmp_path / "ws"
    select_workspace(monkeypatch, workspace)
    conn = _db()
    conn.execute(
        "INSERT INTO projects (id, name, path) VALUES ('p1', 'My Project', ?)",
        (project_ref("custom-dir"),),
    )

    # Basename of projects.path wins over slug(name); stays under the workspace.
    assert _project_dir("p1", workspace, conn) == workspace / "projects" / "custom-dir"


def test_project_dir_is_none_for_absolute_path_rows(tmp_path):
    """An absolute projects.path the startup converter kept is never resolved (a
    row scaffolded under a foreign root would point at the wrong directory); it
    has no planning root."""
    from core.skills_registry import _project_dir

    workspace = tmp_path / "ws"
    conn = _db()
    conn.execute(
        "INSERT INTO projects (id, name, path) VALUES "
        "('p1', 'Neighborhood Services', '/Users/someone/other-root/projects/neighborhood-services')"
    )
    assert _project_dir("p1", workspace, conn) is None


def test_project_dir_is_none_when_path_missing(tmp_path):
    """An empty projects.path has no planning root; there is no slug fallback."""
    from core.skills_registry import _project_dir

    workspace = tmp_path / "ws"
    conn = _db()
    conn.execute("INSERT INTO projects (id, name, path) VALUES ('p1', 'Alpha Beta', '')")
    assert _project_dir("p1", workspace, conn) is None


def test_role_skills_are_selected_by_existing_agent_identity(tmp_path):
    from core.skills_registry import resolve_effective_skills

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _skill(
        workspace,
        "context/roles/agent/skills/improve",
        _valid("improve", "agent improvement"),
    )
    _skill(
        workspace,
        "context/roles/conductor/skills/coordinate",
        _valid("coordinate", "conductor coordination"),
    )

    conn = _db()
    conn.execute("INSERT INTO threads (id, agent_enabled) VALUES ('ordinary', 0)")
    conn.execute("INSERT INTO threads (id, agent_enabled) VALUES ('agent', 1)")

    ordinary = resolve_effective_skills(
        thread_id="ordinary",
        project_id=None,
        workspace=workspace,
        db=conn,
    )
    agent = resolve_effective_skills(
        thread_id="agent",
        project_id=None,
        workspace=workspace,
        db=conn,
    )

    assert not [row for row in ordinary.included if row.scope != "system"]
    assert [
        (row.name, row.role)
        for row in agent.included
        if row.scope != "system"
    ] == [("improve", "agent")]


def test_conductor_role_skills_compose_and_project_role_overrides_workspace(tmp_path, monkeypatch):
    from core.skills_registry import resolve_effective_skills

    workspace = tmp_path / "workspace"
    project = workspace / "projects" / "demo"
    project.mkdir(parents=True)
    select_workspace(monkeypatch, workspace)
    _skill(
        workspace,
        "context/roles/agent/skills/shared",
        _valid("shared", "global agent"),
    )
    _skill(
        project,
        "context/roles/agent/skills/shared",
        _valid("shared", "project agent"),
    )
    _skill(
        workspace,
        "context/roles/conductor/skills/conductor-only",
        _valid("conductor-only", "conductor only"),
    )

    conn = _db()
    conn.execute(
        "INSERT INTO threads (id, project_id, agent_enabled) VALUES (?, ?, 0)",
        ("conductor", "project-a"),
    )
    conn.execute(
        "INSERT INTO projects (id, name, path, conductor_thread_id) "
        "VALUES (?, ?, ?, ?)",
        ("project-a", "Demo", project_ref("demo"), "conductor"),
    )

    resolved = resolve_effective_skills(
        thread_id="conductor",
        project_id="project-a",
        workspace=workspace,
        db=conn,
    )

    included = {row.name: row for row in resolved.included}
    assert included["shared"].description == "project agent"
    assert included["shared"].role == "agent"
    assert included["shared"].scope == "project"
    assert included["conductor-only"].role == "conductor"
    assert any(
        row.name == "shared"
        and row.description == "global agent"
        and row.role == "agent"
        for row in resolved.omitted
    )


def test_thread_skill_overrides_role_skill_without_role_becoming_a_scope(tmp_path):
    from core.skills_registry import resolve_effective_skills

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _skill(
        workspace,
        "context/roles/agent/skills/review",
        _valid("review", "agent review"),
    )
    _skill(
        workspace,
        ".threads/agent/skills/review",
        _valid("review", "thread review"),
    )

    conn = _db()
    conn.execute("INSERT INTO threads (id, agent_enabled) VALUES ('agent', 1)")

    resolved = resolve_effective_skills(
        thread_id="agent",
        project_id=None,
        workspace=workspace,
        db=conn,
    )

    assert [
        (row.description, row.scope, row.role)
        for row in resolved.included
        if row.scope != "system"
    ] == [("thread review", "thread", None)]
    assert [(row.description, row.scope, row.role) for row in resolved.omitted] == [
        ("agent review", "workspace", "agent")
    ]


def test_project_base_skill_overrides_workspace_role_skill_by_existing_scope_precedence(tmp_path, monkeypatch):
    from core.skills_registry import resolve_effective_skills

    workspace = tmp_path / "workspace"
    project = workspace / "projects" / "demo"
    project.mkdir(parents=True)
    select_workspace(monkeypatch, workspace)
    _skill(
        workspace,
        "context/roles/agent/skills/review",
        _valid("review", "global Agent review"),
    )
    _skill(project, "skills/review", _valid("review", "project review"))

    conn = _db()
    conn.execute(
        "INSERT INTO projects (id, name, path) VALUES (?, ?, ?)",
        ("project-a", "Demo", project_ref("demo")),
    )
    conn.execute(
        "INSERT INTO threads (id, project_id, agent_enabled) VALUES (?, ?, 1)",
        ("agent", "project-a"),
    )

    resolved = resolve_effective_skills(
        thread_id="agent",
        project_id="project-a",
        workspace=workspace,
        db=conn,
    )

    assert [
        (row.description, row.scope, row.role)
        for row in resolved.included
        if row.scope != "system"
    ] == [("project review", "project", None)]
    assert [(row.description, row.scope, row.role) for row in resolved.omitted] == [
        ("global Agent review", "workspace", "agent")
    ]


def test_invalid_role_skill_root_is_reported_as_an_error_source(tmp_path):
    from core.skills_registry import skill_context_sources

    workspace = tmp_path / "workspace"
    external = tmp_path / "external-agent-role"
    external.mkdir()
    roles_root = workspace / "context" / "roles"
    roles_root.mkdir(parents=True)
    (roles_root / "agent").symlink_to(external, target_is_directory=True)
    conn = _db()
    conn.execute("INSERT INTO threads (id, agent_enabled) VALUES ('agent', 1)")

    sources = skill_context_sources(
        thread_id="agent",
        project_id=None,
        workspace=workspace,
        db=conn,
    )

    error = next(source for source in sources if source.source_id.startswith("skill-error:"))
    assert error.inclusion == "omitted"
    assert error.freshness == "error"
    assert error.metadata["role"] == "agent"
    assert "cannot be a symlink" in error.metadata["errors"][0]


def test_corsia_agent_role_skill_explicitly_overrides_agent_role_in_the_same_scope(tmp_path):
    from core.skills_registry import resolve_effective_skills

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _skill(
        workspace,
        "context/roles/agent/skills/operate",
        _valid("operate", "Agent operation"),
    )
    _skill(
        workspace,
        "context/roles/corsia_agent/skills/operate",
        _valid("operate", "CORSIA agent operation"),
    )
    conn = _db()
    conn.execute("INSERT INTO threads (id, agent_enabled) VALUES ('conductor', 0)")
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
        ("corsia_agent_thread_id", '{"session_id": "conductor"}', "t"),
    )

    resolved = resolve_effective_skills(
        thread_id="conductor",
        project_id=None,
        workspace=workspace,
        db=conn,
    )

    assert [
        (row.description, row.role)
        for row in resolved.included
        if row.scope != "system"
    ] == [("CORSIA agent operation", "corsia_agent")]
    assert [(row.description, row.role) for row in resolved.omitted] == [
        ("Agent operation", "agent")
    ]


def test_skills_index_names_the_read_tool_each_harness_exposes():
    """Claude Code exposes only mcp__corsia__* tools, so a bare read_file is a dead call."""
    from core.skills_registry import _skills_index_content

    from subagents.cc_invocation import ClaudeInvocation, build_claude_cmd
    from tools.catalog import MCP_CORSIA_PREFIX

    content = _skills_index_content([], ["review"])
    launch = build_claude_cmd(
        ClaudeInvocation(binary="claude", permission_mode="default", mcp_config="{}"),
        stream_input=False,
    )

    assert launch[launch.index("--tools") + 1] == f"{MCP_CORSIA_PREFIX}*"
    assert f"`{MCP_CORSIA_PREFIX}read_file` on Claude Code" in content
    assert "`read_file` on other harnesses" in content
    assert "Use read_file" not in content
