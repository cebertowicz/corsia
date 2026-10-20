from __future__ import annotations

from infra.corsia_agent import ensure_corsia_agent, get_corsia_agent, set_corsia_agent
from infra.conductor import (
    ensure_project_conductor, get_project_conductor, get_scope_coordinator,
    set_project_conductor, set_scope_coordinator,
)

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture(autouse=True)
def _ready_caller_model(monkeypatch):
    """The caller's model reads ready; its catalog check is tested on its own."""
    monkeypatch.setattr(
        "llm.delegation_model.eligible_delegation_model",
        lambda selector: ("claude-code/sonnet", None),
    )


@pytest.fixture(autouse=True)
def _no_scaffold_ownership(monkeypatch):
    """These hand-built schemas omit the artifact registry; enforcement is tested
    on a real schema in test_project_creation_transactions."""
    monkeypatch.setattr(
        "api.project_scaffolding.enforce_scaffold_ownership",
        lambda *args, **kwargs: (),
    )


def _select_workspace(monkeypatch, api_scaffolding, workspace, *, workspace_id="test"):
    from infra.platform_paths import WorkspaceSelection

    monkeypatch.setattr(api_scaffolding.config, "WORKSPACE", workspace)
    monkeypatch.setattr(
        api_scaffolding.config,
        "WORKSPACE_SELECTION",
        WorkspaceSelection(
            workspace_id=workspace_id,
            root=workspace,
            name="Test Workspace",
        ),
    )


def test_create_project_tool_basic_shape():
    from tools.projects import CreateProjectTool

    tool = CreateProjectTool()
    assert tool.name == "project_create"
    params = tool.parameters
    assert params["type"] == "object"
    assert "name" in params["required"]
    assert set(params["properties"]) >= {"name", "description", "status", "repo_path"}
    assert "BRIEF.md" not in tool.description
    assert "working/" not in tool.description


def test_status_enum_in_sync_with_valid_project_statuses():
    from tools.projects import CreateProjectTool
    from api.projects import VALID_PROJECT_STATUSES

    tool = CreateProjectTool()
    enum_values = set(tool.parameters["properties"]["status"]["enum"])
    assert enum_values == set(VALID_PROJECT_STATUSES), (
        f"status enum {enum_values} != VALID_PROJECT_STATUSES {VALID_PROJECT_STATUSES}"
    )


def test_execute_does_not_raise_import_error(monkeypatch, tmp_path):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects ("
        "  id TEXT PRIMARY KEY,"
        "  name TEXT NOT NULL UNIQUE,"
        "  path TEXT,"
        "  description TEXT,"
        "  status TEXT,"
        "  accent_color TEXT,"
        "  doc_roots TEXT,"
        "  bootloader_files TEXT,"
        "  conductor_thread_id TEXT,"
        "  created_at TEXT,"
        "  updated_at TEXT"
        ")"
    )
    conn.execute(
        "CREATE TABLE threads ("
        "  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT, model TEXT,"
        "  reasoning_effort TEXT, project_id TEXT, local_only INTEGER NOT NULL DEFAULT 0,"
        "  local_only_root_thread_id TEXT REFERENCES threads(id),"
        "  created_at TEXT, updated_at TEXT, runtime_context_epoch INTEGER DEFAULT 0,"
        "  last_context_change_seq INTEGER DEFAULT 0"
        ")"
    )
    conn.execute(
        "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)"
    )
    conn.execute("CREATE TABLE context_change_journal (seq INTEGER PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE project_inference_policies ("
        "project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE, "
        "cloud_models_off INTEGER NOT NULL DEFAULT 0 CHECK (cloud_models_off IN (0,1))"
        ")"
    )
    # The conductor's model binds its vendor account from the catalog; an
    # empty catalog reads that model's vendor as unknown, so nothing binds.
    conn.execute(
        "CREATE TABLE models (id TEXT PRIMARY KEY, litellm_model TEXT, harness TEXT,"
        "  provider TEXT, provider_model_id TEXT, available INTEGER)"
    )
    conn.execute(
        "INSERT INTO threads (id, user_id, title, created_at, updated_at) "
        "VALUES ('caller', 'owner', 'Caller', 't', 't')"
    )
    conn.commit()

    class _NoClose:
        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        def close(self):  # no-op for the test
            pass

    wrapped = _NoClose(conn)

    import infra.database

    monkeypatch.setattr(infra.database, "get_db", lambda: wrapped)

    import api.project_scaffolding as api_scaffolding

    workspace = tmp_path / "workspace"
    _select_workspace(
        monkeypatch,
        api_scaffolding,
        workspace,
        workspace_id="tool-workspace",
    )
    monkeypatch.setattr(
        api_scaffolding,
        "sync_bootload_documents",
        lambda *_args, **_kwargs: None,
    )

    from tools.projects import CreateProjectTool

    tool = CreateProjectTool()
    result = asyncio.run(
        tool.execute(name="__test_create_project__", description="t", _thread_id="caller")
    )

    assert "ImportError" not in result, f"#91 regression: {result}"
    payload = json.loads(result)
    assert payload["created"] is True
    assert payload["name"] == "__test_create_project__"
    assert payload["status"] == "active"
    assert payload["workspace_path"] == str(
        workspace / "projects" / "__test_create_project__"
    )
    stored = conn.execute(
        "SELECT path FROM projects WHERE id = ?",
        (payload["project_id"],),
    ).fetchone()["path"]
    assert stored == "workspace://tool-workspace/projects/__test_create_project__"


def test_project_create_survives_and_reports_registration_failure(
    isolated_db,
    monkeypatch,
    tmp_path,
):
    """A failed context-file registration is logged and counted; the project exists."""
    from api import project_scaffolding as api_scaffolding
    from infra.database import get_db
    from registry import bootload
    from tools.projects import CreateProjectTool

    workspace = tmp_path / "workspace"
    _select_workspace(monkeypatch, api_scaffolding, workspace, workspace_id="tool-workspace")
    monkeypatch.setattr(
        api_scaffolding,
        "sync_bootload_documents",
        lambda *_args, **_kwargs: RuntimeError("registry down"),
    )
    events = []
    monkeypatch.setattr(
        bootload, "emit_product_event", lambda *args, **kwargs: events.append((args, kwargs))
    )
    from loguru import logger

    from infra import accounts

    seed = get_db()
    try:
        accounts.create_user("owner", "long-enough-password", db=seed)
        seed.execute(
            "INSERT INTO threads (id,user_id,title,created_at,updated_at)"
            " VALUES ('caller', 'owner', 'Caller', 't', 't')"
        )
        seed.commit()
    finally:
        seed.close()

    lines = []
    sink = logger.add(lines.append, format="{message}")
    try:
        result = asyncio.run(
            CreateProjectTool().execute(
                name="__test_registration_failure__", description="t", _thread_id="caller"
            )
        )
    finally:
        logger.remove(sink)

    payload = json.loads(result)
    assert payload["created"] is True
    check = get_db()
    try:
        stored = check.execute(
            "SELECT id FROM projects WHERE id = ?", (payload["project_id"],)
        ).fetchone()
    finally:
        check.close()
    assert stored is not None
    assert any(
        f"bootload_registry_sync_failed project_id={payload['project_id']}" in str(x)
        for x in lines
    )
    assert events == [
        (
            ("bootload", "registry_sync_failed"),
            {
                "properties": {
                    "project_id": payload["project_id"],
                    "source": "project_create",
                    "error": "RuntimeError",
                }
            },
        )
    ]


def test_project_create_tool_designates_project_conductor_for_calling_thread(
    isolated_db,
    monkeypatch,
    tmp_path,
):
    from api import project_scaffolding as api_scaffolding
    from infra import accounts
    from infra.conductor import get_scope_coordinator
    from infra.database import get_db
    from tools.projects import CreateProjectTool

    workspace = tmp_path / "workspace"
    _select_workspace(monkeypatch, api_scaffolding, workspace, workspace_id="tool-workspace")
    monkeypatch.setattr(
        api_scaffolding,
        "sync_bootload_documents",
        lambda *_args, **_kwargs: None,
    )

    db = get_db()
    try:
        accounts.create_user("owner", "long-enough-password", db=db)
        db.execute(
            "INSERT INTO threads (id,user_id,title,model,created_at,updated_at)"
            " VALUES ('caller', 'owner', 'Caller', 'claude-code/sonnet', 't', 't')"
        )
        db.commit()
    finally:
        db.close()

    result = asyncio.run(
        CreateProjectTool().execute(name="Tool Project", _thread_id="caller")
    )
    payload = json.loads(result)
    assert payload["conductor_model"] == "claude-code/sonnet"

    db = get_db()
    try:
        conductor_id = get_project_conductor(payload["project_id"], db=db)
        row = db.execute(
            "SELECT user_id,title,project_id,model FROM threads WHERE id=?",
            (conductor_id,),
        ).fetchone()
        assert conductor_id == payload["conductor_thread_id"]
        assert dict(row) == {
            "user_id": "owner",
            "title": "Conductor",
            "project_id": payload["project_id"],
            "model": "claude-code/sonnet",
        }
    finally:
        db.close()


def test_execute_returns_loud_collision_error(monkeypatch, tmp_path):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects ("
        "  id TEXT PRIMARY KEY,"
        "  name TEXT NOT NULL UNIQUE,"
        "  path TEXT,"
        "  description TEXT,"
        "  status TEXT,"
        "  accent_color TEXT,"
        "  doc_roots TEXT,"
        "  bootloader_files TEXT,"
        "  conductor_thread_id TEXT,"
        "  created_at TEXT,"
        "  updated_at TEXT"
        ")"
    )
    conn.execute(
        "CREATE TABLE threads ("
        "  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT, model TEXT,"
        "  reasoning_effort TEXT, project_id TEXT, local_only INTEGER NOT NULL DEFAULT 0,"
        "  local_only_root_thread_id TEXT REFERENCES threads(id),"
        "  created_at TEXT, updated_at TEXT, runtime_context_epoch INTEGER DEFAULT 0,"
        "  last_context_change_seq INTEGER DEFAULT 0"
        ")"
    )
    conn.execute(
        "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)"
    )
    conn.execute("CREATE TABLE context_change_journal (seq INTEGER PRIMARY KEY)")
    conn.execute(
        "INSERT INTO projects (id, name, path, description, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "aaaaaaaa",
            "__test_collision__",
            None,
            "",
            "active",
            "2026-05-22",
            "2026-05-22",
        ),
    )
    conn.commit()

    class _NoClose:
        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        def close(self):
            pass

    wrapped = _NoClose(conn)

    import infra.database

    monkeypatch.setattr(infra.database, "get_db", lambda: wrapped)

    import api.project_scaffolding as api_scaffolding

    monkeypatch.setattr(
        api_scaffolding,
        "_scaffold_project_workspace",
        lambda *a, **k: pytest.fail("scaffold must not run when name collides"),
    )

    from tools.projects import CreateProjectTool

    tool = CreateProjectTool()
    result = asyncio.run(tool.execute(name="__test_collision__"))
    assert result.startswith("❌"), f"expected loud-failure ❌ prefix, got: {result!r}"
    assert "already exists" in result


def test_initialize_project_rolls_back_on_scaffold_failure(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects ("
        "  id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, path TEXT, description TEXT,"
        "  status TEXT, accent_color TEXT, doc_roots TEXT, bootloader_files TEXT, created_at TEXT, updated_at TEXT)"
    )
    conn.commit()

    import api.project_scaffolding as api_scaffolding

    def _boom(*a, **k):
        raise RuntimeError("scaffold blew up")

    monkeypatch.setattr(api_scaffolding, "_scaffold_project_workspace", _boom)

    with pytest.raises(RuntimeError):
        api_scaffolding.initialize_project(
            conn, name="__atomic_test__", description="x", user_id="owner",
            conductor_model=None,
        )

    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0, (
        "scaffold failure left an orphaned project row"
    )


@pytest.mark.parametrize("name", ["Foo/Bar", "../shared"])
def test_initialize_project_rejects_non_leaf_name_without_residue(
    monkeypatch,
    tmp_path,
    name,
):
    import api.project_scaffolding as api_scaffolding

    workspace = tmp_path / "workspace"
    (workspace / "projects").mkdir(parents=True)
    (workspace / "shared").mkdir()
    sentinel = workspace / "shared" / "existing.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    _select_workspace(monkeypatch, api_scaffolding, workspace)
    monkeypatch.setattr(
        api_scaffolding,
        "sync_bootload_documents",
        lambda *_args, **_kwargs: None,
    )

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects ("
        "id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, path TEXT, description TEXT, "
        "status TEXT, accent_color TEXT, doc_roots TEXT, bootloader_files TEXT, "
        "created_at TEXT, updated_at TEXT)"
    )
    before = sorted(path.relative_to(workspace) for path in workspace.rglob("*"))

    with pytest.raises(ValueError, match="single directory name"):
        api_scaffolding.initialize_project(
            conn, name=name, user_id="owner", conductor_model=None,
        )

    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
    assert (
        sorted(path.relative_to(workspace) for path in workspace.rglob("*")) == before
    )
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_initialize_project_stores_logical_truth_and_seeds_only_project_context(
    monkeypatch,
    tmp_path,
):
    from infra.platform_paths import WorkspaceSelection
    from api import project_scaffolding, projects

    workspace = tmp_path / "workspace"
    selection = WorkspaceSelection(
        workspace_id="workspace-test",
        root=workspace,
        name="Test Workspace",
    )
    _select_workspace(monkeypatch, project_scaffolding, workspace)
    monkeypatch.setattr(
        project_scaffolding.config,
        "WORKSPACE_SELECTION",
        selection,
    )
    synced: list[tuple[str, Path, list[str]]] = []
    monkeypatch.setattr(
        project_scaffolding,
        "sync_bootload_documents",
        lambda project_id, root, files, **_kwargs: synced.append(
            (project_id, root, files)
        ),
    )

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects ("
        "id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, path TEXT, description TEXT, "
        "status TEXT, accent_color TEXT, doc_roots TEXT, bootloader_files TEXT, "
        "conductor_thread_id TEXT, created_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE threads ("
        "id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT, model TEXT, "
        "reasoning_effort TEXT, project_id TEXT, local_only INTEGER NOT NULL DEFAULT 0, "
        "local_only_root_thread_id TEXT REFERENCES threads(id), "
        "created_at TEXT, updated_at TEXT, runtime_context_epoch INTEGER DEFAULT 0, "
        "last_context_change_seq INTEGER DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)"
    )
    conn.execute("CREATE TABLE context_change_journal (seq INTEGER PRIMARY KEY)")

    project = project_scaffolding.initialize_project(
        conn,
        name="Canonical Project",
        description="A clean project",
        user_id="owner",
        conductor_model=None,
    )

    root = workspace / "projects" / "canonical-project"
    logical = "workspace://workspace-test/projects/canonical-project"
    stored = conn.execute(
        "SELECT path, bootloader_files FROM projects WHERE id = ?",
        (project["id"],),
    ).fetchone()
    assert stored["path"] == logical
    assert project["path"] == logical
    assert json.loads(stored["bootloader_files"]) == []
    assert synced == [
        (
            project["id"],
            root,
            ["context/PROJECT.md", "context/COORDINATION-MODEL.md"],
        )
    ]
    assert (root / "context" / "PROJECT.md").is_file()
    assert (root / "context" / "COORDINATION-MODEL.md").is_file()
    assert not (root / "context" / "BRIEF.md").exists()
    assert not (root / "working").exists()
    assert projects._project_api_projection(project, db=conn)["path"] == str(root)


def test_initialize_project_registers_default_repo_policy(
    monkeypatch,
    tmp_path,
):
    import api.project_scaffolding as api_scaffolding

    workspace = tmp_path / "workspace"
    repo = tmp_path / "repo"
    subprocess.run(
        [
            "git",
            "-c",
            "init.defaultRefFormat=files",
            "init",
            "-b",
            "stable",
            str(repo),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    _select_workspace(monkeypatch, api_scaffolding, workspace)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        api_scaffolding,
        "sync_bootload_documents",
        lambda *_args, **_kwargs: None,
    )

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            path TEXT,
            description TEXT,
            status TEXT,
            accent_color TEXT,
            doc_roots TEXT,
            bootloader_files TEXT,
            conductor_thread_id TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            title TEXT,
            model TEXT,
            reasoning_effort TEXT,
            project_id TEXT,
            local_only INTEGER NOT NULL DEFAULT 0,
            local_only_root_thread_id TEXT REFERENCES threads(id),
            created_at TEXT,
            updated_at TEXT,
            runtime_context_epoch INTEGER DEFAULT 0,
            last_context_change_seq INTEGER DEFAULT 0
        );
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        );
        CREATE TABLE context_change_journal (seq INTEGER PRIMARY KEY);
        CREATE TABLE project_repo_policies (
            project_id TEXT NOT NULL,
            repo_name TEXT NOT NULL,
            repo_path TEXT NOT NULL,
            pinned_branch TEXT,
            protect_base_checkout INTEGER NOT NULL,
            PRIMARY KEY (project_id, repo_name)
        );
        """
    )

    real_pin_check = api_scaffolding.branch_pin_support_error

    def pin_check_without_writer(repo_path):
        assert not conn.in_transaction, "Git inspection must precede the project writer"
        return real_pin_check(repo_path)

    monkeypatch.setattr(api_scaffolding, "branch_pin_support_error", pin_check_without_writer)
    project = api_scaffolding.initialize_project(
        conn,
        name="Demo",
        repo_path_str=str(repo),
        user_id="owner",
        conductor_model=None,
    )

    policy = conn.execute(
        "SELECT repo_name, repo_path, pinned_branch, protect_base_checkout "
        "FROM project_repo_policies WHERE project_id = ?",
        (project["id"],),
    ).fetchone()
    assert tuple(policy) == ("repo", str(repo.resolve()), "stable", 1)


@pytest.mark.asyncio
async def test_project_create_api_rejects_repo_registration_during_owned_process(
    monkeypatch,
    tmp_path,
):
    from api import project_scaffolding, projects
    from fastapi import HTTPException

    workspace = tmp_path / "workspace"
    repo = tmp_path / "repo"
    repo.mkdir()
    _select_workspace(monkeypatch, project_scaffolding, workspace)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(projects, "authenticate", lambda _token: {"id": "user-1"})
    monkeypatch.setattr(
        project_scaffolding,
        "_active_owned_process_count",
        lambda: 1,
        raising=False,
    )

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            path TEXT,
            description TEXT,
            status TEXT,
            accent_color TEXT,
            doc_roots TEXT,
            bootloader_files TEXT,
            conductor_thread_id TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            title TEXT,
            model TEXT,
            reasoning_effort TEXT,
            project_id TEXT,
            local_only INTEGER NOT NULL DEFAULT 0,
            local_only_root_thread_id TEXT REFERENCES threads(id),
            created_at TEXT,
            updated_at TEXT,
            runtime_context_epoch INTEGER DEFAULT 0,
            last_context_change_seq INTEGER DEFAULT 0
        );
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        );
        CREATE TABLE context_change_journal (seq INTEGER PRIMARY KEY);
        CREATE TABLE project_repo_policies (
            project_id TEXT NOT NULL,
            repo_name TEXT NOT NULL,
            repo_path TEXT NOT NULL,
            pinned_branch TEXT,
            protect_base_checkout INTEGER NOT NULL,
            PRIMARY KEY (project_id, repo_name)
        );
        """
    )

    class _NoClose:
        def __getattr__(self, name):
            return getattr(conn, name)

        def close(self):
            pass

    async def _run_now(callback):
        return callback()

    monkeypatch.setattr(projects, "get_db", _NoClose)
    monkeypatch.setattr(projects, "run_in_db", _run_now)

    with pytest.raises(HTTPException) as raised:
        await projects.create_project(
            "token",
            projects.ProjectCreate(name="Demo", repo_path=str(repo)),
        )

    assert raised.value.status_code == 409
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
    entry = workspace / "projects" / "demo" / "source" / "repo"
    assert not entry.exists()
    assert not entry.is_symlink()


def test_initialize_project_removes_only_its_link_when_policy_write_fails(
    monkeypatch,
    tmp_path,
):
    import api.project_scaffolding as api_scaffolding

    workspace = tmp_path / "workspace"
    repo = tmp_path / "repo"
    repo.mkdir()
    _select_workspace(monkeypatch, api_scaffolding, workspace)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        api_scaffolding,
        "register_prepared_repo_policy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("policy failed")),
        raising=False,
    )

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            path TEXT,
            description TEXT,
            status TEXT,
            accent_color TEXT,
            doc_roots TEXT,
            bootloader_files TEXT,
            conductor_thread_id TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            title TEXT,
            model TEXT,
            reasoning_effort TEXT,
            project_id TEXT,
            local_only INTEGER NOT NULL DEFAULT 0,
            local_only_root_thread_id TEXT REFERENCES threads(id),
            created_at TEXT,
            updated_at TEXT,
            runtime_context_epoch INTEGER DEFAULT 0,
            last_context_change_seq INTEGER DEFAULT 0
        );
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        );
        CREATE TABLE context_change_journal (seq INTEGER PRIMARY KEY);
        CREATE TABLE project_repo_policies (
            project_id TEXT NOT NULL,
            repo_name TEXT NOT NULL,
            repo_path TEXT NOT NULL,
            pinned_branch TEXT,
            protect_base_checkout INTEGER NOT NULL,
            PRIMARY KEY (project_id, repo_name)
        );
        """
    )

    with pytest.raises(RuntimeError, match="policy failed"):
        api_scaffolding.initialize_project(
            conn,
            name="Demo",
            repo_path_str=str(repo),
            user_id="owner",
            conductor_model=None,
        )

    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
    assert not (workspace / "projects" / "demo" / "source" / "repo").is_symlink()


def test_initialize_project_replacement_before_ownership_capture_is_not_removed(
    monkeypatch,
    tmp_path,
):
    import api.project_scaffolding as api_scaffolding

    workspace = tmp_path / "workspace"
    registered = tmp_path / "registered"
    replacement = tmp_path / "replacement"
    registered.mkdir()
    replacement.mkdir()
    _select_workspace(monkeypatch, api_scaffolding, workspace)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    real_link = api_scaffolding.link_repo_into_source

    def _link_then_replace(*args, **kwargs):
        result = real_link(*args, **kwargs)
        row = result[0] if isinstance(result, tuple) else result
        entry = workspace / "projects" / "demo" / "source" / row["name"]
        entry.unlink()
        entry.symlink_to(replacement, target_is_directory=True)
        return result

    monkeypatch.setattr(
        api_scaffolding,
        "link_repo_into_source",
        _link_then_replace,
    )

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            path TEXT,
            description TEXT,
            status TEXT,
            accent_color TEXT,
            doc_roots TEXT,
            bootloader_files TEXT,
            conductor_thread_id TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            title TEXT,
            model TEXT,
            reasoning_effort TEXT,
            project_id TEXT,
            local_only INTEGER NOT NULL DEFAULT 0,
            local_only_root_thread_id TEXT REFERENCES threads(id),
            created_at TEXT,
            updated_at TEXT,
            runtime_context_epoch INTEGER DEFAULT 0,
            last_context_change_seq INTEGER DEFAULT 0
        );
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        );
        CREATE TABLE context_change_journal (seq INTEGER PRIMARY KEY);
        CREATE TABLE project_repo_policies (
            project_id TEXT NOT NULL,
            repo_name TEXT NOT NULL,
            repo_path TEXT NOT NULL,
            pinned_branch TEXT,
            protect_base_checkout INTEGER NOT NULL,
            PRIMARY KEY (project_id, repo_name)
        );
        """
    )

    with pytest.raises(RuntimeError, match="source binding.*changed"):
        api_scaffolding.initialize_project(
            conn,
            name="Demo",
            repo_path_str=str(registered),
            user_id="owner",
            conductor_model=None,
        )

    entry = workspace / "projects" / "demo" / "source" / "registered"
    assert entry.is_symlink()
    assert entry.resolve() == replacement.resolve()
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0


def test_initialize_project_rechecks_source_ownership_before_commit(
    monkeypatch,
    tmp_path,
):
    import api.project_scaffolding as api_scaffolding

    workspace = tmp_path / "workspace"
    registered = tmp_path / "registered"
    replacement = tmp_path / "replacement"
    registered.mkdir()
    replacement.mkdir()
    _select_workspace(monkeypatch, api_scaffolding, workspace)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    real_write = api_scaffolding._write_doc_roots

    def _write_then_replace(db, project_id, roots):
        real_write(db, project_id, roots)
        entry = workspace / "projects" / "demo" / "source" / "registered"
        entry.unlink()
        entry.symlink_to(replacement, target_is_directory=True)

    monkeypatch.setattr(
        api_scaffolding,
        "_write_doc_roots",
        _write_then_replace,
    )

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            path TEXT,
            description TEXT,
            status TEXT,
            accent_color TEXT,
            doc_roots TEXT,
            bootloader_files TEXT,
            conductor_thread_id TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            title TEXT,
            model TEXT,
            reasoning_effort TEXT,
            project_id TEXT,
            local_only INTEGER NOT NULL DEFAULT 0,
            local_only_root_thread_id TEXT REFERENCES threads(id),
            created_at TEXT,
            updated_at TEXT,
            runtime_context_epoch INTEGER DEFAULT 0,
            last_context_change_seq INTEGER DEFAULT 0
        );
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        );
        CREATE TABLE context_change_journal (seq INTEGER PRIMARY KEY);
        CREATE TABLE project_repo_policies (
            project_id TEXT NOT NULL,
            repo_name TEXT NOT NULL,
            repo_path TEXT NOT NULL,
            pinned_branch TEXT,
            protect_base_checkout INTEGER NOT NULL,
            PRIMARY KEY (project_id, repo_name)
        );
        """
    )

    with pytest.raises(RuntimeError, match="source binding.*changed"):
        api_scaffolding.initialize_project(
            conn,
            name="Demo",
            repo_path_str=str(registered),
            user_id="owner",
            conductor_model=None,
        )

    entry = workspace / "projects" / "demo" / "source" / "registered"
    assert entry.is_symlink()
    assert entry.resolve() == replacement.resolve()
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0


def test_a_created_project_nudges_every_tab_of_its_owner(isolated_db, monkeypatch, tmp_path):
    """Tool and route both create through one path that emits one after-commit nudge."""
    from api import project_scaffolding as api_scaffolding
    from infra import accounts, state
    from infra.database import get_db
    from tools.projects import CreateProjectTool

    _select_workspace(monkeypatch, api_scaffolding, tmp_path / "workspace")
    monkeypatch.setattr(api_scaffolding, "sync_bootload_documents", lambda *a, **k: None)
    seed = get_db()
    try:
        accounts.create_user("owner", "long-enough-password", db=seed)
        seed.execute(
            "INSERT INTO threads (id,user_id,title,created_at,updated_at)"
            " VALUES ('caller', 'owner', 'Caller', 't', 't')"
        )
        seed.commit()
    finally:
        seed.close()
    sent = []

    def _broadcast(user_id, message):
        check = get_db()
        try:
            committed = check.execute("SELECT 1 FROM projects WHERE name='Nudged'").fetchone()
        finally:
            check.close()
        sent.append((user_id, message, committed is not None))

    monkeypatch.setattr(state, "broadcast_to_user", _broadcast)

    asyncio.run(CreateProjectTool().execute(name="Nudged", _thread_id="caller"))

    assert sent == [("owner", {"type": "projects_changed"}, True)]


def test_a_failed_nudge_does_not_fail_the_created_project(isolated_db, monkeypatch, tmp_path):
    from api import project_scaffolding as api_scaffolding
    from infra import accounts, state
    from infra.database import get_db
    from tools.projects import CreateProjectTool

    _select_workspace(monkeypatch, api_scaffolding, tmp_path / "workspace")
    monkeypatch.setattr(api_scaffolding, "sync_bootload_documents", lambda *a, **k: None)
    seed = get_db()
    try:
        accounts.create_user("owner", "long-enough-password", db=seed)
        seed.execute(
            "INSERT INTO threads (id,user_id,title,created_at,updated_at)"
            " VALUES ('caller', 'owner', 'Caller', 't', 't')"
        )
        seed.commit()
    finally:
        seed.close()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("socket gone")

    monkeypatch.setattr(state, "broadcast_to_user", _boom)

    payload = json.loads(asyncio.run(CreateProjectTool().execute(name="Quiet", _thread_id="caller")))

    assert payload["created"] is True
