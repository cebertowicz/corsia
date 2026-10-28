"""Tests for the agent-facing project_repo_register tool.

The tool is a thin front door onto ``api.project_repos.register_source_repo``,
so these drive the real shared procedure against a real SQLite database and a
real on-disk repo rather than stubbing the flow out. What is asserted here is
the agent-surface contract: only Git repositories, no agent-settable pin, and
the durable rows/bindings the shared procedure is responsible for.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from tools.project_repo_register import ProjectRepoRegisterTool
from tests.github_screen_schema import SCREEN_INSTALL_SCHEMA_SQL


class _NoClose:
    def __init__(self, db: sqlite3.Connection):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self):
        pass


def _git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-c", "init.defaultRefFormat=files", "init", "-b", "main", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(path), "add", "tracked.txt"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git", "-C", str(path),
            "-c", "user.name=CORSIA Test",
            "-c", "user.email=test@corsia.local",
            "commit", "-m", "base",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _policy_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:", timeout=2, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(SCREEN_INSTALL_SCHEMA_SQL)
    db.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT,
            path TEXT,
            doc_roots TEXT,
            conductor_thread_id TEXT
        );
        CREATE TABLE IF NOT EXISTS project_repo_policies (
            project_id TEXT NOT NULL,
            repo_name TEXT NOT NULL,
            repo_path TEXT NOT NULL,
            pinned_branch TEXT,
            protect_base_checkout INTEGER NOT NULL,
            PRIMARY KEY (project_id, repo_name)
        )
        """
    )
    db.execute(
        "INSERT INTO projects (id, name, path, doc_roots) "
        "VALUES ('p1', 'Demo', '/tmp/demo', '[]')"
    )
    db.commit()
    return db


async def _run_now(callback):
    return callback()


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Point the shared procedure at an isolated workspace + database.

    Only the process-wide seams are redirected (home, workspace dir, DB
    handle, DB thread). The add-repo procedure, its transition guard, its
    locking, and its compensation all run for real.
    """
    from api import project_repos

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    workspace = tmp_path / "projects" / "demo"
    workspace.mkdir(parents=True, exist_ok=True)
    db = _policy_db()
    monkeypatch.setattr(project_repos, "_workspace_dir_for", lambda _id: workspace)
    monkeypatch.setattr(project_repos, "get_db", lambda: _NoClose(db))
    monkeypatch.setattr("tools.project_repo_register.run_in_db", _run_now, raising=False)
    monkeypatch.setattr(
        "infra.db_async.run_in_db", _run_now, raising=False,
    )
    return {"db": db, "workspace": workspace, "tmp": tmp_path}


@pytest.mark.asyncio
async def test_register_links_git_repo_pinned_to_current_branch(wired) -> None:
    repo = wired["tmp"] / "external" / "app"
    _git_repo(repo)

    result = await ProjectRepoRegisterTool().execute(
        repo_path=str(repo),
        _thread_id="thread-1",
        _thread_project_id="p1",
    )

    assert "Registered repository 'app'" in result
    assert "pinned=main protected" in result
    row = wired["db"].execute(
        "SELECT * FROM project_repo_policies WHERE repo_name = 'app'"
    ).fetchone()
    assert row["pinned_branch"] == "main"
    assert row["protect_base_checkout"] == 1
    assert (wired["workspace"] / "source" / "app").is_symlink()


@pytest.mark.asyncio
async def test_register_honours_explicit_repo_name(wired) -> None:
    repo = wired["tmp"] / "external" / "app"
    _git_repo(repo)

    result = await ProjectRepoRegisterTool().execute(
        repo_path=str(repo),
        repo_name="custom",
        _thread_id="thread-1",
        _thread_project_id="p1",
    )

    assert "Registered repository 'custom'" in result
    assert (wired["workspace"] / "source" / "custom").is_symlink()


@pytest.mark.asyncio
async def test_register_refuses_non_git_directory(wired) -> None:
    """The agent surface attaches repositories, never arbitrary folders."""
    plain = wired["tmp"] / "secrets"
    plain.mkdir(parents=True, exist_ok=True)
    (plain / "id_rsa").write_text("private\n", encoding="utf-8")

    result = await ProjectRepoRegisterTool().execute(
        repo_path=str(plain),
        _thread_id="thread-1",
        _thread_project_id="p1",
    )

    assert result.startswith("Error:")
    assert "not a Git repository" in result
    assert not (wired["workspace"] / "source" / "secrets").exists()
    assert wired["db"].execute(
        "SELECT COUNT(*) FROM project_repo_policies"
    ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_register_takes_no_pin_or_protection_argument() -> None:
    """Registering can never switch a branch in the user's real checkout."""
    properties = ProjectRepoRegisterTool().parameters["properties"]
    assert set(properties) == {"repo_path", "repo_name"}


@pytest.mark.asyncio
async def test_register_requires_project_and_path() -> None:
    tool = ProjectRepoRegisterTool()
    no_project = await tool.execute(
        repo_path="/tmp/somewhere",
        _thread_id="thread-1",
    )
    assert no_project.startswith("Error:")
    assert "current project" in no_project

    no_path = await tool.execute(
        _thread_id="thread-1",
        _thread_project_id="p1",
    )
    assert no_path.startswith("Error:")
    assert "repo_path" in no_path


@pytest.mark.asyncio
async def test_register_missing_path_fails_before_any_effect(wired) -> None:
    missing = wired["tmp"] / "does-not-exist"

    result = await ProjectRepoRegisterTool().execute(
        repo_path=str(missing),
        _thread_id="thread-1",
        _thread_project_id="p1",
    )

    assert result.startswith("Error:")
    assert wired["db"].execute(
        "SELECT COUNT(*) FROM project_repo_policies"
    ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_register_same_repo_twice_is_refused_and_leaves_one_row(
    wired,
) -> None:
    """Re-registering is refused by the source binding, not silently rewritten."""
    repo = wired["tmp"] / "external" / "app"
    _git_repo(repo)

    first = await ProjectRepoRegisterTool().execute(
        repo_path=str(repo),
        _thread_id="thread-1",
        _thread_project_id="p1",
    )
    assert first.startswith("Registered repository")

    second = await ProjectRepoRegisterTool().execute(
        repo_path=str(repo),
        _thread_id="thread-1",
        _thread_project_id="p1",
    )

    assert second.startswith("Error:")
    assert "already exists" in second
    rows = wired["db"].execute(
        "SELECT repo_name, repo_path FROM project_repo_policies"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["repo_name"] == "app"


@pytest.mark.asyncio
async def test_register_outside_home_is_refused(wired, tmp_path_factory) -> None:
    """The shared procedure's confinement still applies through this surface."""
    outside = tmp_path_factory.mktemp("outside") / "app"
    _git_repo(outside)

    result = await ProjectRepoRegisterTool().execute(
        repo_path=str(outside),
        _thread_id="thread-1",
        _thread_project_id="p1",
    )

    assert result.startswith("Error:")
    assert wired["db"].execute(
        "SELECT COUNT(*) FROM project_repo_policies"
    ).fetchone()[0] == 0


# --- BLK2: the registry must not make this tool collide with itself ---------


async def _run_inline(fn, *args, **kwargs):
    """Run one DB job on this thread, so the whole call keeps one connection."""
    return fn(*args, **kwargs)


async def _registry_register(
    monkeypatch,
    db,
    workspace: Path,
    repo_path: str,
    *,
    via_tool_call: bool = False,
):
    """Run one project_repo_register through the real ToolRegistry.

    Only the process-wide seams move (home, workspace, DB handle, the
    host-action gate's decision, and the DB thread). The registry's own
    repository-effect admission, the transition, the lock and the
    compensation all run for real.
    """
    from contextlib import asynccontextmanager

    from api import project_repos
    from infra import state
    from tools import host_access_gate
    from tools.registry import ToolRegistry

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: workspace.parent.parent))
    monkeypatch.setattr(project_repos, "_workspace_dir_for", lambda _id: workspace)
    monkeypatch.setattr(project_repos, "get_db", lambda: _NoClose(db))
    monkeypatch.setattr(project_repos, "_sync", lambda *_args: None)
    monkeypatch.setattr(project_repos, "run_in_db", _run_inline)
    monkeypatch.setattr("infra.db_async.run_in_db", _run_inline, raising=False)
    monkeypatch.setattr("tools.dispatch_ownership.run_in_db", _run_inline, raising=False)

    async def authorize(**_kwargs):
        return host_access_gate.HostActionAuthorization()

    monkeypatch.setattr(host_access_gate, "authorize_host_action", authorize)

    db.execute(
        "INSERT INTO threads (id,user_id,model,project_id,created_at,updated_at) "
        "VALUES ('reg-thread','u-test','test/model','p1','t','t')"
    )
    db.commit()

    registry = ToolRegistry()
    registry.register(ProjectRepoRegisterTool())
    token = state._current_thread_id.set("reg-thread")
    try:
        if via_tool_call:
            return await registry.execute("tool_call", {
                "name": "project_repo_register",
                "arguments": {"repo_path": repo_path},
            })
        return await registry.execute("project_repo_register", {"repo_path": repo_path})
    finally:
        state._current_thread_id.reset(token)


@pytest.fixture
def registry_env(isolated_db, tmp_path):
    """A real schema database plus the project/repo rows the tool needs."""
    from infra.database import get_db

    workspace = tmp_path / "projects" / "demo"
    (workspace / "source").mkdir(parents=True)
    conn = get_db()
    conn.execute(
        "INSERT INTO projects (id,name,path,created_at,updated_at) "
        "VALUES ('p1','Demo',?,'t','t')",
        (str(workspace),),
    )
    conn.commit()
    env = {"db": conn, "workspace": workspace, "tmp": tmp_path}
    yield env
    conn.close()


@pytest.mark.parametrize("via_tool_call", [False, True])
@pytest.mark.asyncio
async def test_registry_lets_project_repo_register_complete(registry_env, monkeypatch, via_tool_call):
    """Matching the resolved name means a tool_call dispatch is exempted too."""
    repo = registry_env["tmp"] / "external" / "app"
    _git_repo(repo)

    result = await _registry_register(
        monkeypatch, registry_env["db"], registry_env["workspace"],
        str(repo), via_tool_call=via_tool_call,
    )

    assert "Registered repository 'app'" in result, result
    assert registry_env["db"].execute(
        "SELECT COUNT(*) FROM project_repo_policies"
    ).fetchone()[0] == 1
    assert (registry_env["workspace"] / "source" / "app").is_symlink()


@pytest.mark.parametrize("blocker", ["effect", "owned process"])
@pytest.mark.asyncio
async def test_registry_still_refuses_register_while_repo_access_is_active(
    registry_env, monkeypatch, blocker,
):
    """The exemption removes only self-collision, never real exclusion."""
    from infra.repo_policy_transition import admit_repo_effect
    from api import project_repos

    repo = registry_env["tmp"] / "external" / "app"
    _git_repo(repo)
    if blocker == "owned process":
        monkeypatch.setattr(project_repos, "_active_owned_process_count", lambda: 1)

    held = admit_repo_effect() if blocker == "effect" else None
    if held is not None:
        held.__enter__()
    try:
        result = await _registry_register(
            monkeypatch, registry_env["db"], registry_env["workspace"], str(repo),
        )
    finally:
        if held is not None:
            held.__exit__(None, None, None)

    expected = (
        "Repository policy transition is blocked by an active tool effect."
        if blocker == "effect"
        else "Repository policy transition is blocked by an active owned process."
    )
    assert expected in result, result
    assert registry_env["db"].execute(
        "SELECT COUNT(*) FROM project_repo_policies"
    ).fetchone()[0] == 0
    assert not (registry_env["workspace"] / "source" / "app").exists()


@pytest.mark.asyncio
async def test_registry_runs_non_git_refusal_before_the_transition(registry_env, monkeypatch):
    """The folder check is read-only and still precedes any transition work."""
    from infra.repo_policy_transition import admit_repo_effect

    plain = registry_env["tmp"] / "secrets"
    plain.mkdir(parents=True)
    (plain / "id_rsa").write_text("private\n", encoding="utf-8")

    with admit_repo_effect():
        result = await _registry_register(
            monkeypatch, registry_env["db"], registry_env["workspace"], str(plain),
        )

    assert result.startswith("Error:")
    assert "not a Git repository" in result, result
    assert "Repository policy transition" not in result
