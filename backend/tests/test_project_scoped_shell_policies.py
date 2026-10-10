from __future__ import annotations

import sqlite3
import subprocess
from functools import partial
from pathlib import Path

from infra.project_repo_policy import list_registered_repo_policies
from tests.project_ref_helpers import project_ref, select_workspace
from tools import shell_guard
from tools.shell_policy import load_shell_repo_policies


def _git(*args: str) -> None:
    subprocess.run(
        ["/usr/bin/git", *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _repo_with_worktree(path: Path) -> Path:
    _git(
        "-c",
        "init.defaultRefFormat=files",
        "init",
        "-b",
        "main",
        str(path),
    )
    _git(
        "-C",
        str(path),
        "-c",
        "user.name=CORSIA Test",
        "-c",
        "user.email=test@corsia.local",
        "commit",
        "--allow-empty",
        "-m",
        "initial",
    )
    worktree = path / ".worktrees" / "feature"
    _git(
        "-C",
        str(path),
        "worktree",
        "add",
        "-b",
        "feature",
        str(worktree),
    )
    return path.resolve()


def _project(
    db: sqlite3.Connection,
    workspace: Path,
    project_id: str,
    name: str,
) -> Path:
    root = workspace / "projects" / project_id
    (root / "source").mkdir(parents=True)
    db.execute(
        "INSERT INTO projects (id, name, path) VALUES (?, ?, ?)",
        (project_id, name, project_ref(project_id)),
    )
    return root


def _register(
    db: sqlite3.Connection,
    project: Path,
    project_id: str,
    repo_name: str,
    repo: Path,
    *,
    bind_source: bool = True,
) -> None:
    if bind_source:
        (project / "source" / repo_name).symlink_to(
            repo,
            target_is_directory=True,
        )
    db.execute(
        "INSERT INTO project_repo_policies "
        "(project_id, repo_name, repo_path, pinned_branch, "
        "protect_base_checkout) VALUES (?, ?, ?, 'main', 1)",
        (project_id, repo_name, str(repo)),
    )


def _deny(repo: Path) -> str:
    return f'(deny file-write* (subpath "{repo}"))'


def _git_allow(repo: Path) -> str:
    return f'(allow file-read* file-write* (subpath "{repo / ".git"}"))'


def test_project_filter_precedes_coalescing_and_global_denies_remain(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    select_workspace(monkeypatch, workspace)
    shared_repo = _repo_with_worktree(tmp_path / "shared")
    other_repo = _repo_with_worktree(tmp_path / "other")
    stale_repo = _repo_with_worktree(tmp_path / "stale")

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            path TEXT
        );
        CREATE TABLE project_repo_policies (
            project_id TEXT NOT NULL,
            repo_name TEXT NOT NULL,
            repo_path TEXT NOT NULL,
            pinned_branch TEXT,
            protect_base_checkout INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (project_id, repo_name)
        );
        """
    )
    other_project = _project(db, workspace, "a-other", "Other")
    invoking_project = _project(db, workspace, "z-invoking", "Invoking")
    _register(db, other_project, "a-other", "shared", shared_repo)
    _register(db, other_project, "a-other", "other", other_repo)
    _register(db, invoking_project, "z-invoking", "shared", shared_repo)
    _register(
        db,
        invoking_project,
        "z-invoking",
        "stale",
        stale_repo,
        bind_source=False,
    )

    loader = partial(
        list_registered_repo_policies,
        workspace=workspace,
        db=db,
    )
    policies, grants = load_shell_repo_policies(
        loader,
        leased=False,
        project_id="z-invoking",
    )
    profile = shell_guard.pinned_repo_sandbox_profile(
        "pytest",
        str(workspace),
        policies=policies,
        grant_policies=grants,
    )

    global_shared = next(policy for policy in policies if policy.path == shared_repo)
    scoped_shared = next(policy for policy in grants if policy.path == shared_repo)
    assert global_shared.project_id == "a-other"
    assert scoped_shared.project_id == "z-invoking"
    assert _git_allow(shared_repo) in profile
    assert _git_allow(other_repo) not in profile
    assert _git_allow(stale_repo) not in profile
    assert _deny(shared_repo) in profile
    assert _deny(other_repo) in profile
    assert _deny(stale_repo) in profile
