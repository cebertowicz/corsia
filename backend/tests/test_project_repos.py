
from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from api.project_scaffolding import (  # noqa: E402
    compute_doc_roots,
    create_repo_in_source,
    link_repo_into_source,
    list_source_repos,
    remove_linked_repo,
)
from auth_fixtures import OWNER_TOKEN  # noqa: E402

_TOKEN = OWNER_TOKEN


def _unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _init_git_repo(path: Path) -> None:
    subprocess.run(
        ["git", "-c", "init.defaultRefFormat=files", "init", "-b", "main", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )


def _git_repo_with_commit(
    path: Path,
    *,
    branches: tuple[str, ...] = (),
    detached: bool = False,
) -> None:
    _init_git_repo(path)
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(path), "add", "tracked.txt"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=CORSIA Test",
            "-c",
            "user.email=test@corsia.local",
            "commit",
            "-m",
            "base",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    for branch in branches:
        subprocess.run(
            ["git", "-C", str(path), "branch", branch],
            check=True,
            capture_output=True,
            text=True,
        )
    if detached:
        subprocess.run(
            ["git", "-C", str(path), "switch", "--detach", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )


def _policy_db(path: str | Path = ":memory:") -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=2, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            doc_roots TEXT
        );
        CREATE TABLE project_repo_policies (
            project_id TEXT NOT NULL,
            repo_name TEXT NOT NULL,
            repo_path TEXT NOT NULL,
            pinned_branch TEXT,
            protect_base_checkout INTEGER NOT NULL,
            PRIMARY KEY (project_id, repo_name)
        )
        """
    )
    return db


def _insert_policy(
    db: sqlite3.Connection,
    repo: Path,
    *,
    pinned_branch: str | None = "main",
    protect_base_checkout: bool = True,
) -> None:
    db.execute(
        "INSERT INTO project_repo_policies VALUES (?, ?, ?, ?, ?)",
        (
            "p1",
            "app",
            str(repo.resolve()),
            pinned_branch,
            int(protect_base_checkout),
        ),
    )
    db.commit()


class _NoClose:
    def __init__(self, db: sqlite3.Connection):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self):
        pass


async def _run_now(callback):
    return callback()


def _configure_repo_api(monkeypatch, module, workspace: Path, db) -> None:
    monkeypatch.setattr(module, "authenticate", lambda _token: True)
    monkeypatch.setattr(module, "_workspace_dir_for", lambda _id: workspace)
    monkeypatch.setattr(module, "run_in_db", _run_now)
    monkeypatch.setattr(module, "get_db", lambda: _NoClose(db))
    monkeypatch.setattr(module, "_sync", lambda *_args: None)
    monkeypatch.setattr("events.observe_rules.sync_shipped_rules", lambda *_args: None)


_HOST_LOAD_DEADLINE_S = 120


@pytest.fixture(autouse=True)
def _git_deadlines_ignore_host_load(monkeypatch):
    """Real Git reads here must not time out because the host is busy (#3314).

    The production deadlines (5-30s) are for a responsive machine; a loaded CI
    runner can exceed them on a trivial ``git config``, turning a conflict test
    into a 503. Timeout behavior is tested by injecting ``TimeoutExpired``.
    """
    real_run = subprocess.run

    def _run(*args, **kwargs):
        if kwargs.get("timeout") is not None:
            kwargs["timeout"] = max(kwargs["timeout"], _HOST_LOAD_DEADLINE_S)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _run)


@pytest.fixture
def home_is_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))


@pytest.mark.asyncio
async def test_list_repos_loads_full_policies_once(monkeypatch, tmp_path):
    from api import project_repos
    from infra.project_repo_policy import RepoPolicy

    repos = [
        {"name": "app", "path": str(tmp_path / "app"), "git": {"is_repo": True}},
        {"name": "lib", "path": str(tmp_path / "lib"), "git": {"is_repo": True}},
    ]
    batch_reads = []

    def _get_policies(project_id):
        batch_reads.append(project_id)
        return {
            "app": RepoPolicy(
                project_id,
                "app",
                (tmp_path / "app").resolve(),
                None,
                False,
            )
        }

    monkeypatch.setattr(project_repos, "authenticate", lambda _token: True)
    monkeypatch.setattr(project_repos, "_workspace_dir_for", lambda _id: tmp_path)
    monkeypatch.setattr(project_repos, "list_source_repos", lambda _path: repos)
    monkeypatch.setattr(project_repos, "run_in_db", _run_now)
    monkeypatch.setattr(
        project_repos,
        "get_project_repo_policies",
        _get_policies,
        raising=False,
    )
    response = await project_repos.list_repos("token", "p1")

    assert response == {
        "repos": [
            {
                "name": "app",
                "path": str(tmp_path / "app"),
                "git": {"is_repo": True},
                "registered": True,
                "pinned_branch": None,
                "protect_base_checkout": False,
                "branch_pinned": False,
            },
            {
                "name": "lib",
                "path": str(tmp_path / "lib"),
                "git": {"is_repo": True},
                "registered": False,
                "pinned_branch": None,
                "protect_base_checkout": False,
                "branch_pinned": False,
            },
        ]
    }
    assert batch_reads == ["p1"]


@pytest.mark.asyncio
async def test_repin_rejects_unsupported_git_metadata_without_changing_override(
    monkeypatch, tmp_path
):
    from fastapi import HTTPException
    from api import project_repos
    from infra.project_repo_policy import RepoPolicy

    repo = tmp_path / "linked"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: /tmp/shared.git/worktrees/linked\n")
    writes = []
    db = _policy_db()
    _configure_repo_api(monkeypatch, project_repos, tmp_path, db)
    monkeypatch.setattr(
        project_repos,
        "list_source_repos",
        lambda _path: [
            {
                "name": "app",
                "path": str(repo),
                "git": {"is_repo": True},
            }
        ],
    )
    monkeypatch.setattr(
        project_repos,
        "get_project_repo_policies",
        lambda _project_id, **_kwargs: {
            "app": RepoPolicy("p1", "app", repo.resolve(), None, False)
        },
    )
    monkeypatch.setattr(
        project_repos,
        "register_repo_policy",
        lambda *args, **kwargs: writes.append((args, kwargs)),
        raising=False,
    )

    with pytest.raises(HTTPException) as raised:
        await project_repos.update_repo(
            "token",
            "p1",
            "app",
            project_repos.RepoUpdate(pinned_branch="main"),
        )

    assert raised.value.status_code == 409
    assert "standalone" in str(raised.value.detail).lower()
    assert writes == []
    db.close()


@pytest.mark.asyncio
async def test_add_repo_can_register_unsupported_git_metadata_explicitly_unpinned(
    monkeypatch, tmp_path, home_is_tmp
):
    from api import project_repos

    workspace = tmp_path / "project"
    workspace.mkdir()
    repo = tmp_path / "linked-worktree"
    repo.mkdir()
    (repo / ".git").write_text(
        "gitdir: /tmp/shared.git/worktrees/linked\n",
        encoding="utf-8",
    )
    db = _policy_db()
    _configure_repo_api(monkeypatch, project_repos, workspace, db)

    result = await project_repos.add_repo(
        "token",
        "p1",
        project_repos.RepoAdd(
            mode="link",
            path=str(repo),
            pinned_branch=None,
            protect_base_checkout=False,
        ),
    )

    assert (workspace / "source" / "linked-worktree").resolve() == repo.resolve()
    assert result["repo"]["registered"] is True
    assert result["repo"]["pinned_branch"] is None
    assert result["repo"]["protect_base_checkout"] is False
    assert result["repo"]["branch_pinned"] is False
    db.close()


@pytest.mark.asyncio
async def test_add_repo_compensates_source_link_when_policy_write_fails(
    monkeypatch,
    tmp_path,
    home_is_tmp,
):
    from api import project_repos

    workspace = tmp_path / "project"
    workspace.mkdir()
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    db = _policy_db()
    _configure_repo_api(monkeypatch, project_repos, workspace, db)
    monkeypatch.setattr(
        project_repos,
        "register_repo_policy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("db failed")),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="db failed"):
        await project_repos.add_repo(
            "token",
            "p1",
            project_repos.RepoAdd(mode="link", path=str(repo), name="app"),
        )

    assert not (workspace / "source" / "app").exists()
    assert not (workspace / "source" / "app").is_symlink()
    db.close()


@pytest.mark.asyncio
async def test_add_repo_response_failure_rolls_back_policy_and_source_link(
    monkeypatch,
    tmp_path,
    home_is_tmp,
):
    from api import project_repos

    workspace = tmp_path / "project"
    workspace.mkdir()
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    db = _policy_db()
    db.execute(
        "INSERT INTO projects (id, doc_roots) VALUES ('p1', 'before')"
    )
    db.commit()
    real_sync = project_repos._sync
    _configure_repo_api(monkeypatch, project_repos, workspace, db)
    monkeypatch.setattr(project_repos, "_sync", real_sync)
    monkeypatch.setattr(
        project_repos,
        "_with_policy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("response failed")
        ),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="response failed"):
        await project_repos.add_repo(
            "token",
            "p1",
            project_repos.RepoAdd(
                mode="link",
                path=str(repo),
                name="app",
                pinned_branch=None,
                protect_base_checkout=False,
            ),
        )

    assert db.execute("SELECT COUNT(*) FROM project_repo_policies").fetchone()[0] == 0
    assert db.execute(
        "SELECT doc_roots FROM projects WHERE id = 'p1'"
    ).fetchone()[0] == "before"
    assert not (workspace / "source" / "app").exists()
    assert not (workspace / "source" / "app").is_symlink()
    db.close()


@pytest.mark.asyncio
async def test_add_repo_rejects_registration_while_repo_effect_is_active(
    monkeypatch,
    tmp_path,
    home_is_tmp,
):
    from api import project_repos
    from fastapi import HTTPException
    from infra.repo_policy_transition import admit_repo_effect

    workspace = tmp_path / "project"
    workspace.mkdir()
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    db = _policy_db()
    _configure_repo_api(monkeypatch, project_repos, workspace, db)

    with admit_repo_effect():
        with pytest.raises(HTTPException) as raised:
            await project_repos.add_repo(
                "token",
                "p1",
                project_repos.RepoAdd(mode="link", path=str(repo), name="app"),
            )

    assert raised.value.status_code == 409
    assert db.execute("SELECT COUNT(*) FROM project_repo_policies").fetchone()[0] == 0
    assert not (workspace / "source" / "app").exists()
    assert not (workspace / "source" / "app").is_symlink()
    db.close()


@pytest.mark.asyncio
async def test_add_repo_rejects_registration_while_owned_process_is_active(
    monkeypatch,
    tmp_path,
    home_is_tmp,
):
    from api import project_repos
    from fastapi import HTTPException

    workspace = tmp_path / "project"
    workspace.mkdir()
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    db = _policy_db()
    _configure_repo_api(monkeypatch, project_repos, workspace, db)
    monkeypatch.setattr(
        project_repos,
        "_active_owned_process_count",
        lambda: 1,
    )

    with pytest.raises(HTTPException) as raised:
        await project_repos.add_repo(
            "token",
            "p1",
            project_repos.RepoAdd(mode="link", path=str(repo), name="app"),
        )

    assert raised.value.status_code == 409
    assert db.execute("SELECT COUNT(*) FROM project_repo_policies").fetchone()[0] == 0
    assert not (workspace / "source" / "app").exists()
    assert not (workspace / "source" / "app").is_symlink()
    db.close()


@pytest.mark.asyncio
async def test_detach_repo_restores_exact_link_when_policy_delete_fails(
    monkeypatch,
    tmp_path,
    home_is_tmp,
):
    from api import project_repos

    workspace = tmp_path / "project"
    source = workspace / "source"
    repo = workspace / "repo"
    source.mkdir(parents=True)
    repo.mkdir()
    entry = source / "app"
    raw_target = Path("..") / "repo"
    entry.symlink_to(raw_target, target_is_directory=True)
    db = _policy_db()
    _insert_policy(db, repo, pinned_branch=None, protect_base_checkout=False)
    _configure_repo_api(monkeypatch, project_repos, workspace, db)
    monkeypatch.setattr(
        project_repos,
        "clear_repo_policy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("delete failed")),
    )

    with pytest.raises(RuntimeError, match="delete failed"):
        await project_repos.detach_repo("token", "p1", "app")

    assert entry.is_symlink()
    assert entry.readlink() == raw_target
    assert entry.resolve() == repo.resolve()
    db.close()


@pytest.mark.parametrize("blocker", ["effect", "process"])
@pytest.mark.asyncio
async def test_detach_repo_rejects_while_repo_access_is_active(
    monkeypatch,
    tmp_path,
    home_is_tmp,
    blocker,
):
    from contextlib import nullcontext

    from api import project_repos
    from fastapi import HTTPException
    from infra.repo_policy_transition import admit_repo_effect

    workspace = tmp_path / "project"
    source = workspace / "source"
    source.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    entry = source / "app"
    entry.symlink_to(repo, target_is_directory=True)
    db = _policy_db()
    _insert_policy(
        db,
        repo,
        pinned_branch=None,
        protect_base_checkout=False,
    )
    _configure_repo_api(monkeypatch, project_repos, workspace, db)
    if blocker == "process":
        monkeypatch.setattr(
            project_repos,
            "_active_owned_process_count",
            lambda: 1,
        )
    active_effect = admit_repo_effect() if blocker == "effect" else nullcontext()

    with active_effect:
        with pytest.raises(HTTPException) as raised:
            await project_repos.detach_repo("token", "p1", "app")

    assert raised.value.status_code == 409
    assert entry.is_symlink()
    assert entry.resolve() == repo.resolve()
    assert db.execute("SELECT COUNT(*) FROM project_repo_policies").fetchone()[0] == 1
    db.close()


@pytest.mark.asyncio
async def test_detach_replacement_returns_conflict_without_clobbering(
    monkeypatch,
    tmp_path,
    home_is_tmp,
):
    from api import project_repos
    from fastapi import HTTPException

    workspace = tmp_path / "project"
    source = workspace / "source"
    registered = tmp_path / "registered"
    replacement = tmp_path / "replacement"
    source.mkdir(parents=True)
    registered.mkdir()
    replacement.mkdir()
    (source / "app").symlink_to(registered, target_is_directory=True)
    db = _policy_db()
    _insert_policy(db, registered, pinned_branch=None, protect_base_checkout=False)
    real_clear = project_repos.clear_repo_policy

    def _clear_then_replace(*args, **kwargs):
        real_clear(*args, **kwargs)
        (source / "app").symlink_to(replacement, target_is_directory=True)

    _configure_repo_api(monkeypatch, project_repos, workspace, db)
    monkeypatch.setattr(
        project_repos,
        "clear_repo_policy",
        _clear_then_replace,
    )

    with pytest.raises(HTTPException) as raised:
        await project_repos.detach_repo("token", "p1", "app")

    assert raised.value.status_code == 409
    assert db.execute("SELECT COUNT(*) FROM project_repo_policies").fetchone()[0] == 1
    assert (source / "app").resolve() == replacement.resolve()
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback_fails", [False, True])
async def test_settings_policy_failure_rolls_back_controlled_branch_switch(
    monkeypatch,
    tmp_path,
    rollback_fails,
):
    from api import project_repos
    from infra.project_repo_policy import current_repo_branch

    workspace = tmp_path / "project"
    source = workspace / "source"
    source.mkdir(parents=True)
    repo = tmp_path / "repo"
    _git_repo_with_commit(repo, branches=("release",))
    (source / "app").symlink_to(repo, target_is_directory=True)
    db = _policy_db()
    _insert_policy(db, repo)
    _configure_repo_api(monkeypatch, project_repos, workspace, db)
    monkeypatch.setattr(
        project_repos,
        "register_repo_policy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("policy failed")
        ),
        raising=False,
    )
    if rollback_fails:
        monkeypatch.setattr(
            project_repos,
            "restore_repo_branch_switch",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("restore failed")
            ),
        )

    error_match = (
        "rollback also failed.*restore failed"
        if rollback_fails
        else "policy failed"
    )
    with pytest.raises(RuntimeError, match=error_match):
        await project_repos.update_repo(
            "token",
            "p1",
            "app",
            project_repos.RepoUpdate(pinned_branch="release"),
        )

    assert current_repo_branch(repo) == (
        "release" if rollback_fails else "main"
    )
    db.close()


@pytest.mark.asyncio
async def test_update_repo_response_failure_restores_policy_and_branch(
    monkeypatch,
    tmp_path,
):
    from api import project_repos
    from infra.project_repo_policy import current_repo_branch

    workspace = tmp_path / "project"
    source = workspace / "source"
    source.mkdir(parents=True)
    repo = tmp_path / "repo"
    _git_repo_with_commit(repo, branches=("release",))
    (source / "app").symlink_to(repo, target_is_directory=True)
    db = _policy_db()
    _insert_policy(db, repo)
    _configure_repo_api(monkeypatch, project_repos, workspace, db)
    monkeypatch.setattr(
        project_repos,
        "_with_policy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("response failed")
        ),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="response failed"):
        await project_repos.update_repo(
            "token",
            "p1",
            "app",
            project_repos.RepoUpdate(pinned_branch="release"),
        )

    stored = db.execute(
        "SELECT pinned_branch, protect_base_checkout "
        "FROM project_repo_policies WHERE project_id = 'p1' AND repo_name = 'app'"
    ).fetchone()
    assert tuple(stored) == ("main", 1)
    assert current_repo_branch(repo) == "main"
    db.close()


@pytest.mark.asyncio
async def test_update_detached_repo_can_change_protection_then_clear_pin(
    monkeypatch,
    tmp_path,
):
    from api import project_repos

    workspace = tmp_path / "project"
    source = workspace / "source"
    source.mkdir(parents=True)
    repo = tmp_path / "repo"
    _git_repo_with_commit(repo)
    (source / "app").symlink_to(repo, target_is_directory=True)
    db = _policy_db()
    _insert_policy(db, repo)
    subprocess.run(
        ["git", "-C", str(repo), "switch", "--detach", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    _configure_repo_api(monkeypatch, project_repos, workspace, db)

    protection = await project_repos.update_repo(
        "token",
        "p1",
        "app",
        project_repos.RepoUpdate(protect_base_checkout=False),
    )
    unpinned = await project_repos.update_repo(
        "token",
        "p1",
        "app",
        project_repos.RepoUpdate(pinned_branch=None),
    )

    assert protection["repo"]["pinned_branch"] == "main"
    assert protection["repo"]["protect_base_checkout"] is False
    assert unpinned["repo"]["pinned_branch"] is None
    assert unpinned["repo"]["protect_base_checkout"] is False
    db.close()


@pytest.mark.asyncio
async def test_update_returns_retryable_error_when_git_status_times_out(
    monkeypatch,
    tmp_path,
):
    from api import project_repos
    from fastapi import HTTPException
    from infra import project_repo_policy

    workspace = tmp_path / "project"
    source = workspace / "source"
    source.mkdir(parents=True)
    repo = tmp_path / "repo"
    _git_repo_with_commit(repo)
    (source / "app").symlink_to(repo, target_is_directory=True)
    db = _policy_db()
    _insert_policy(db, repo)
    _configure_repo_api(monkeypatch, project_repos, workspace, db)
    real_run = project_repo_policy.subprocess.run

    def _timeout_status(command, *args, **kwargs):
        if command[:2] == ["git", "status"]:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(project_repo_policy.subprocess, "run", _timeout_status)

    with pytest.raises(HTTPException) as raised:
        await project_repos.update_repo(
            "token",
            "p1",
            "app",
            project_repos.RepoUpdate(protect_base_checkout=False),
        )

    assert raised.value.status_code == 503
    assert "timed out" in str(raised.value.detail)
    stored = db.execute(
        "SELECT pinned_branch, protect_base_checkout "
        "FROM project_repo_policies"
    ).fetchone()
    assert tuple(stored) == ("main", 1)
    db.close()


@pytest.mark.asyncio
async def test_add_returns_retryable_error_when_git_status_times_out(
    monkeypatch,
    tmp_path,
    home_is_tmp,
):
    from api import project_repos
    from fastapi import HTTPException
    from infra import project_repo_policy
    from infra.project_repo_policy import current_repo_branch

    workspace = tmp_path / "project"
    workspace.mkdir()
    repo = tmp_path / "repo"
    _git_repo_with_commit(repo, branches=("release",))
    db = _policy_db()
    _configure_repo_api(monkeypatch, project_repos, workspace, db)
    real_run = project_repo_policy.subprocess.run

    def _timeout_status(command, *args, **kwargs):
        if command[:2] == ["git", "status"]:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(project_repo_policy.subprocess, "run", _timeout_status)

    with pytest.raises(HTTPException) as raised:
        await project_repos.add_repo(
            "token",
            "p1",
            project_repos.RepoAdd(
                mode="link",
                path=str(repo),
                name="app",
                pinned_branch="release",
            ),
        )

    assert raised.value.status_code == 503
    assert "timed out" in str(raised.value.detail)
    assert current_repo_branch(repo) == "main"
    assert not (workspace / "source" / "app").exists()
    assert not (workspace / "source" / "app").is_symlink()
    assert db.execute("SELECT COUNT(*) FROM project_repo_policies").fetchone()[0] == 0
    db.close()


@pytest.mark.asyncio
async def test_update_returns_conflict_when_git_status_fails(monkeypatch, tmp_path):
    from api import project_repos
    from fastapi import HTTPException
    from infra import project_repo_policy

    workspace = tmp_path / "project"
    source = workspace / "source"
    source.mkdir(parents=True)
    repo = tmp_path / "repo"
    _git_repo_with_commit(repo)
    (source / "app").symlink_to(repo, target_is_directory=True)
    db = _policy_db()
    _insert_policy(db, repo)
    _configure_repo_api(monkeypatch, project_repos, workspace, db)
    real_run = project_repo_policy.subprocess.run

    def _failed_status(command, *args, **kwargs):
        if command[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(command, 1, "", "status failed")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(project_repo_policy.subprocess, "run", _failed_status)
    with pytest.raises(HTTPException) as raised:
        await project_repos.update_repo("token", "p1", "app", project_repos.RepoUpdate(protect_base_checkout=False))
    assert raised.value.status_code == 409
    assert tuple(db.execute("SELECT pinned_branch, protect_base_checkout FROM project_repo_policies").fetchone()) == ("main", 1)
    db.close()


@pytest.mark.asyncio
async def test_update_returns_retryable_error_when_recheck_git_status_times_out(monkeypatch, tmp_path):
    from api import project_repos
    from fastapi import HTTPException
    from infra import project_repo_policy

    workspace = tmp_path / "project"
    source = workspace / "source"
    source.mkdir(parents=True)
    repo = tmp_path / "repo"
    _git_repo_with_commit(repo)
    (source / "app").symlink_to(repo, target_is_directory=True)
    db = _policy_db()
    _insert_policy(db, repo)
    _configure_repo_api(monkeypatch, project_repos, workspace, db)
    real_run = project_repo_policy.subprocess.run
    status_calls = 0

    def _timeout_recheck(command, *args, **kwargs):
        nonlocal status_calls
        if command[:2] == ["git", "status"]:
            status_calls += 1
            if status_calls == 2:
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(project_repo_policy.subprocess, "run", _timeout_recheck)
    with pytest.raises(HTTPException) as raised:
        await project_repos.update_repo("token", "p1", "app", project_repos.RepoUpdate(protect_base_checkout=False))
    assert raised.value.status_code == 503
    assert tuple(db.execute("SELECT pinned_branch, protect_base_checkout FROM project_repo_policies").fetchone()) == ("main", 1)
    db.close()


@pytest.mark.asyncio
async def test_add_returns_retryable_error_when_current_branch_times_out(monkeypatch, tmp_path, home_is_tmp):
    from api import project_repos
    from fastapi import HTTPException
    from infra import project_repo_policy

    workspace = tmp_path / "project"
    workspace.mkdir()
    repo = tmp_path / "repo"
    _git_repo_with_commit(repo)
    db = _policy_db()
    _configure_repo_api(monkeypatch, project_repos, workspace, db)
    real_run = project_repo_policy.subprocess.run

    def _timeout_current_branch(command, *args, **kwargs):
        if command[:2] == ["git", "symbolic-ref"]:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(project_repo_policy.subprocess, "run", _timeout_current_branch)
    with pytest.raises(HTTPException) as raised:
        await project_repos.add_repo("token", "p1", project_repos.RepoAdd(mode="link", path=str(repo), name="app"))
    assert raised.value.status_code == 503
    assert not (workspace / "source" / "app").exists()
    db.close()


@pytest.mark.asyncio
async def test_update_returns_retryable_error_when_pin_support_check_times_out(
    monkeypatch,
    tmp_path,
):
    from api import project_repos
    from fastapi import HTTPException
    from infra import project_repo_policy

    workspace = tmp_path / "project"
    source = workspace / "source"
    source.mkdir(parents=True)
    repo = tmp_path / "repo"
    _git_repo_with_commit(repo, branches=("release",))
    (source / "app").symlink_to(repo, target_is_directory=True)
    db = _policy_db()
    _insert_policy(db, repo, pinned_branch=None, protect_base_checkout=False)
    _configure_repo_api(monkeypatch, project_repos, workspace, db)
    real_run = project_repo_policy.subprocess.run

    def _timeout_pin_support(command, *args, **kwargs):
        if command[:2] == ["git", "config"]:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(project_repo_policy.subprocess, "run", _timeout_pin_support)

    with pytest.raises(HTTPException) as raised:
        await project_repos.update_repo(
            "token",
            "p1",
            "app",
            project_repos.RepoUpdate(pinned_branch="release"),
        )

    assert raised.value.status_code == 503
    assert "Git ref storage verification timed out" in str(raised.value.detail)
    db.close()


@pytest.mark.asyncio
async def test_add_returns_retryable_error_when_pin_support_check_times_out(
    monkeypatch,
    tmp_path,
    home_is_tmp,
):
    from api import project_repos
    from fastapi import HTTPException
    from infra import project_repo_policy

    workspace = tmp_path / "project"
    workspace.mkdir()
    repo = tmp_path / "repo"
    _git_repo_with_commit(repo, branches=("release",))
    db = _policy_db()
    _configure_repo_api(monkeypatch, project_repos, workspace, db)
    real_run = project_repo_policy.subprocess.run

    def _timeout_pin_support(command, *args, **kwargs):
        if command[:2] == ["git", "config"]:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(project_repo_policy.subprocess, "run", _timeout_pin_support)

    with pytest.raises(HTTPException) as raised:
        await project_repos.add_repo(
            "token",
            "p1",
            project_repos.RepoAdd(
                mode="link",
                path=str(repo),
                name="app",
                pinned_branch="release",
            ),
        )

    assert raised.value.status_code == 503
    assert "Git ref storage verification timed out" in str(raised.value.detail)
    assert not (workspace / "source" / "app").exists()
    db.close()


@pytest.mark.asyncio
async def test_branch_list_returns_retryable_error_when_git_times_out(
    monkeypatch,
    tmp_path,
):
    from api import project_repos
    from fastapi import HTTPException
    from infra import project_repo_policy

    workspace = tmp_path / "project"
    source = workspace / "source"
    source.mkdir(parents=True)
    repo = tmp_path / "repo"
    _git_repo_with_commit(repo)
    (source / "app").symlink_to(repo, target_is_directory=True)
    db = _policy_db()
    _insert_policy(db, repo)
    _configure_repo_api(monkeypatch, project_repos, workspace, db)
    real_run = project_repo_policy.subprocess.run

    def _timeout_branch_list(command, *args, **kwargs):
        if command[:2] == ["git", "for-each-ref"]:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(project_repo_policy.subprocess, "run", _timeout_branch_list)

    with pytest.raises(HTTPException) as raised:
        await project_repos.list_branches("token", "p1", "app")

    assert raised.value.status_code == 503
    assert "local branch listing timed out" in str(raised.value.detail)
    db.close()


@pytest.mark.asyncio
async def test_switch_timeout_returns_retryable_error_and_restores_checkout(
    monkeypatch,
    tmp_path,
):
    from api import project_repos
    from fastapi import HTTPException
    from infra import project_repo_policy
    from infra.project_repo_policy import current_repo_branch

    workspace = tmp_path / "project"
    source = workspace / "source"
    source.mkdir(parents=True)
    repo = tmp_path / "repo"
    _git_repo_with_commit(repo, branches=("release",))
    (source / "app").symlink_to(repo, target_is_directory=True)
    db = _policy_db()
    _insert_policy(db, repo, pinned_branch=None, protect_base_checkout=False)
    _configure_repo_api(monkeypatch, project_repos, workspace, db)
    real_run = project_repo_policy.subprocess.run

    def _timeout_after_switch(command, *args, **kwargs):
        result = real_run(command, *args, **kwargs)
        if "switch" in command and command[-1] == "release":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return result

    monkeypatch.setattr(project_repo_policy.subprocess, "run", _timeout_after_switch)

    with pytest.raises(HTTPException) as raised:
        await project_repos.update_repo(
            "token",
            "p1",
            "app",
            project_repos.RepoUpdate(pinned_branch="release"),
        )

    assert raised.value.status_code == 503
    assert "branch switch to release timed out" in str(raised.value.detail)
    assert current_repo_branch(repo) == "main"
    assert subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain=v1"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout == ""
    db.close()


@pytest.mark.asyncio
async def test_update_rejects_activation_while_old_policy_shell_is_in_flight(
    monkeypatch,
    tmp_path,
):
    from api import project_repos
    from fastapi import HTTPException
    from infra.repo_policy_transition import admit_repo_effect

    workspace = tmp_path / "project"
    source = workspace / "source"
    source.mkdir(parents=True)
    repo = tmp_path / "repo"
    _git_repo_with_commit(repo)
    (source / "app").symlink_to(repo, target_is_directory=True)
    db = _policy_db()
    _insert_policy(
        db,
        repo,
        pinned_branch=None,
        protect_base_checkout=False,
    )
    _configure_repo_api(monkeypatch, project_repos, workspace, db)

    with admit_repo_effect():
        with pytest.raises(HTTPException) as raised:
            await project_repos.update_repo(
                "token",
                "p1",
                "app",
                project_repos.RepoUpdate(
                    pinned_branch="main",
                    protect_base_checkout=True,
                ),
            )

    assert raised.value.status_code == 409
    stored = db.execute(
        "SELECT pinned_branch, protect_base_checkout "
        "FROM project_repo_policies"
    ).fetchone()
    assert tuple(stored) == (None, 0)
    db.close()


@pytest.mark.parametrize("mutation", ["dirty", "branch"])
@pytest.mark.asyncio
async def test_update_rechecks_checkout_before_policy_commit(
    monkeypatch,
    tmp_path,
    mutation,
):
    from api import project_repos
    from fastapi import HTTPException
    from infra.repo_policy_transition import admit_repo_effect
    from infra.project_repo_policy import current_repo_branch

    workspace = tmp_path / "project"
    source = workspace / "source"
    source.mkdir(parents=True)
    repo = tmp_path / "repo"
    _git_repo_with_commit(repo, branches=("release",))
    (source / "app").symlink_to(repo, target_is_directory=True)
    db = _policy_db()
    _insert_policy(
        db,
        repo,
        pinned_branch=None,
        protect_base_checkout=False,
    )
    real_register = project_repos.register_repo_policy

    def _register_then_mutate(*args, **kwargs):
        policy = real_register(*args, **kwargs)
        if mutation == "dirty":
            (repo / "tracked.txt").write_text(
                "changed\n",
                encoding="utf-8",
            )
        else:
            subprocess.run(
                ["git", "-C", str(repo), "switch", "release"],
                check=True,
                capture_output=True,
                text=True,
            )
        return policy

    _configure_repo_api(monkeypatch, project_repos, workspace, db)
    monkeypatch.setattr(
        project_repos,
        "register_repo_policy",
        _register_then_mutate,
    )

    with pytest.raises(HTTPException) as raised:
        await project_repos.update_repo(
            "token",
            "p1",
            "app",
            project_repos.RepoUpdate(protect_base_checkout=True),
        )

    assert raised.value.status_code == 409
    assert "checkout changed" in str(raised.value.detail).lower()
    stored = db.execute(
        "SELECT pinned_branch, protect_base_checkout "
        "FROM project_repo_policies"
    ).fetchone()
    assert tuple(stored) == (None, 0)
    if mutation == "branch":
        assert current_repo_branch(repo) == "release"
    with admit_repo_effect():
        pass
    db.close()


@pytest.mark.parametrize(
    ("pinned_branch", "protect_base_checkout"),
    [
        ("main", True),
        ("main", False),
        (None, True),
        (None, False),
    ],
)
@pytest.mark.asyncio
async def test_pin_and_checkout_protection_are_independent(
    monkeypatch,
    tmp_path,
    pinned_branch,
    protect_base_checkout,
):
    from api import project_repos

    workspace = tmp_path / "project"
    source = workspace / "source"
    source.mkdir(parents=True)
    repo = tmp_path / "repo"
    _git_repo_with_commit(repo)
    (source / "app").symlink_to(repo, target_is_directory=True)
    db = _policy_db()
    _insert_policy(db, repo)
    _configure_repo_api(monkeypatch, project_repos, workspace, db)

    result = await project_repos.update_repo(
        "token",
        "p1",
        "app",
        project_repos.RepoUpdate(
            pinned_branch=pinned_branch,
            protect_base_checkout=protect_base_checkout,
        ),
    )

    assert result["repo"]["pinned_branch"] == pinned_branch
    assert (
        result["repo"]["protect_base_checkout"]
        is protect_base_checkout
    )
    assert result["repo"]["branch_pinned"] is (pinned_branch is not None)
    db.close()


@pytest.mark.asyncio
async def test_repo_mutations_for_same_project_are_serialized(
    monkeypatch,
    tmp_path,
    home_is_tmp,
):
    from api import project_repos

    workspace = tmp_path / "project"
    workspace.mkdir()
    first_repo = tmp_path / "first"
    second_repo = tmp_path / "second"
    first_repo.mkdir()
    second_repo.mkdir()
    db_path = tmp_path / "policies.db"
    _policy_db(db_path).close()
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    call_guard = threading.Lock()
    call_count = 0
    real_link = project_repos.link_repo_into_source

    def _controlled_link(*args, **kwargs):
        nonlocal call_count
        with call_guard:
            call_count += 1
            position = call_count
        if position == 1:
            first_entered.set()
            assert release_first.wait(2)
        else:
            second_entered.set()
        return real_link(*args, **kwargs)

    async def _run_in_thread(callback):
        return await asyncio.to_thread(callback)

    def _connection():
        db = sqlite3.connect(db_path, timeout=2)
        db.row_factory = sqlite3.Row
        return db

    monkeypatch.setattr(project_repos, "authenticate", lambda _token: True)
    monkeypatch.setattr(project_repos, "_workspace_dir_for", lambda _id: workspace)
    monkeypatch.setattr(project_repos, "run_in_db", _run_in_thread)
    monkeypatch.setattr(project_repos, "get_db", _connection)
    monkeypatch.setattr(project_repos, "_sync", lambda *_args: None)
    monkeypatch.setattr(project_repos, "link_repo_into_source", _controlled_link)

    first = asyncio.create_task(
        project_repos.add_repo(
            "token",
            "p1",
            project_repos.RepoAdd(
                mode="link",
                path=str(first_repo),
                name="first",
            ),
        )
    )
    assert await asyncio.to_thread(first_entered.wait, 2)
    second = asyncio.create_task(
        project_repos.add_repo(
            "token",
            "p1",
            project_repos.RepoAdd(
                mode="link",
                path=str(second_repo),
                name="second",
            ),
        )
    )

    assert not await asyncio.to_thread(second_entered.wait, 0.2)
    release_first.set()
    await asyncio.gather(first, second)
    assert second_entered.is_set()
    verify = sqlite3.connect(db_path)
    assert verify.execute("SELECT COUNT(*) FROM project_repo_policies").fetchone()[0] == 2
    assert verify.execute(
        "SELECT DISTINCT pinned_branch, protect_base_checkout "
        "FROM project_repo_policies"
    ).fetchall() == [(None, 0)]
    verify.close()


@pytest.mark.asyncio
async def test_update_retarget_before_commit_rolls_back_policy(
    monkeypatch,
    tmp_path,
):
    from api import project_repos
    from fastapi import HTTPException

    workspace = tmp_path / "project"
    source = workspace / "source"
    source.mkdir(parents=True)
    registered = tmp_path / "registered"
    replacement = tmp_path / "replacement"
    _init_git_repo(registered)
    _init_git_repo(replacement)
    link = source / "app"
    link.symlink_to(registered, target_is_directory=True)
    db = _policy_db()
    _insert_policy(db, registered)
    real_register = project_repos.register_repo_policy

    def _register_then_retarget(*args, **kwargs):
        policy = real_register(*args, **kwargs)
        link.unlink()
        link.symlink_to(replacement, target_is_directory=True)
        return policy

    _configure_repo_api(monkeypatch, project_repos, workspace, db)
    monkeypatch.setattr(
        project_repos,
        "register_repo_policy",
        _register_then_retarget,
    )

    with pytest.raises(HTTPException) as raised:
        await project_repos.update_repo(
            "token",
            "p1",
            "app",
            project_repos.RepoUpdate(protect_base_checkout=False),
        )

    assert raised.value.status_code == 409
    stored = db.execute(
        "SELECT repo_path, protect_base_checkout FROM project_repo_policies"
    ).fetchone()
    assert tuple(stored) == (str(registered.resolve()), 1)
    assert link.resolve() == replacement.resolve()
    db.close()


@pytest.mark.asyncio
async def test_add_retarget_before_commit_returns_conflict_without_clobbering(
    monkeypatch,
    tmp_path,
    home_is_tmp,
):
    from api import project_repos
    from fastapi import HTTPException

    workspace = tmp_path / "project"
    workspace.mkdir()
    registered = tmp_path / "registered"
    replacement = tmp_path / "replacement"
    _init_git_repo(registered)
    _init_git_repo(replacement)
    db = _policy_db()
    real_register = project_repos.register_repo_policy

    def _register_then_retarget(*args, **kwargs):
        policy = real_register(*args, **kwargs)
        link = workspace / "source" / "app"
        link.unlink()
        link.symlink_to(replacement, target_is_directory=True)
        return policy

    _configure_repo_api(monkeypatch, project_repos, workspace, db)
    monkeypatch.setattr(
        project_repos,
        "register_repo_policy",
        _register_then_retarget,
    )

    with pytest.raises(HTTPException) as raised:
        await project_repos.add_repo(
            "token",
            "p1",
            project_repos.RepoAdd(
                mode="link",
                path=str(registered),
                name="app",
            ),
        )

    assert raised.value.status_code == 409
    assert db.execute("SELECT COUNT(*) FROM project_repo_policies").fetchone()[0] == 0
    assert (workspace / "source" / "app").resolve() == replacement.resolve()
    db.close()


@pytest.mark.asyncio
async def test_add_replacement_before_ownership_capture_is_not_removed(
    monkeypatch,
    tmp_path,
    home_is_tmp,
):
    from api import project_repos
    from fastapi import HTTPException

    workspace = tmp_path / "project"
    workspace.mkdir()
    registered = tmp_path / "registered"
    replacement = tmp_path / "replacement"
    _init_git_repo(registered)
    _init_git_repo(replacement)
    db = _policy_db()
    real_link = project_repos.link_repo_into_source

    def _link_then_replace(*args, **kwargs):
        result = real_link(*args, **kwargs)
        row = result[0] if isinstance(result, tuple) else result
        entry = workspace / "source" / row["name"]
        entry.unlink()
        entry.symlink_to(replacement, target_is_directory=True)
        return result

    _configure_repo_api(monkeypatch, project_repos, workspace, db)
    monkeypatch.setattr(
        project_repos,
        "link_repo_into_source",
        _link_then_replace,
    )

    with pytest.raises(HTTPException) as raised:
        await project_repos.add_repo(
            "token",
            "p1",
            project_repos.RepoAdd(
                mode="link",
                path=str(registered),
                name="app",
            ),
        )

    assert raised.value.status_code == 409
    entry = workspace / "source" / "app"
    assert entry.is_symlink()
    assert entry.resolve() == replacement.resolve()
    assert db.execute("SELECT COUNT(*) FROM project_repo_policies").fetchone()[0] == 0
    db.close()


@pytest.mark.asyncio
async def test_detach_replacement_before_unlink_is_not_removed(
    monkeypatch,
    tmp_path,
    home_is_tmp,
):
    from api import project_repos
    from fastapi import HTTPException

    workspace = tmp_path / "project"
    source = workspace / "source"
    registered = tmp_path / "registered"
    replacement = tmp_path / "replacement"
    source.mkdir(parents=True)
    registered.mkdir()
    replacement.mkdir()
    entry = source / "app"
    entry.symlink_to(registered, target_is_directory=True)
    db = _policy_db()
    _insert_policy(db, registered, pinned_branch=None, protect_base_checkout=False)
    real_remove = project_repos.remove_linked_repo

    def _replace_then_remove(*args, **kwargs):
        entry.unlink()
        entry.symlink_to(replacement, target_is_directory=True)
        return real_remove(*args, **kwargs)

    _configure_repo_api(monkeypatch, project_repos, workspace, db)
    monkeypatch.setattr(
        project_repos,
        "remove_linked_repo",
        _replace_then_remove,
    )

    with pytest.raises(HTTPException) as raised:
        await project_repos.detach_repo("token", "p1", "app")

    assert raised.value.status_code == 409
    assert entry.is_symlink()
    assert entry.resolve() == replacement.resolve()
    assert db.execute("SELECT COUNT(*) FROM project_repo_policies").fetchone()[0] == 1
    db.close()


def test_native_repo_creation_never_adopts_replacement_during_git_init(
    monkeypatch,
    tmp_path,
):
    from api import project_scaffolding

    workspace = tmp_path / "project"
    workspace.mkdir()
    real_run = project_scaffolding.subprocess.run

    def _run_then_replace(*args, **kwargs):
        result = real_run(*args, **kwargs)
        exposed = Path(kwargs["cwd"])
        shutil.rmtree(exposed)
        exposed.mkdir()
        (exposed / "replacement.txt").write_text(
            "must survive\n",
            encoding="utf-8",
        )
        return result

    monkeypatch.setattr(
        project_scaffolding.subprocess,
        "run",
        _run_then_replace,
    )

    with pytest.raises(
        project_scaffolding.SourceEntryChangedError,
        match="source binding.*changed",
    ):
        project_scaffolding.create_repo_in_source(
            workspace,
            "app",
            with_ownership=True,
        )

    public = workspace / "source" / "app"
    assert not public.exists()
    staged = list((workspace / "source").glob(".app.corsia-native-*"))
    assert len(staged) == 1
    assert (staged[0] / "replacement.txt").read_text(encoding="utf-8") == (
        "must survive\n"
    )


def test_atomic_claim_restore_never_overwrites_new_public_entry(
    monkeypatch,
    tmp_path,
):
    from api import project_scaffolding, source_entry_ownership

    source = tmp_path / "source"
    source.mkdir()
    original = tmp_path / "original"
    replacement = tmp_path / "replacement"
    newcomer = tmp_path / "newcomer"
    original.mkdir()
    replacement.mkdir()
    newcomer.mkdir()
    entry = source / "app"
    entry.symlink_to(original, target_is_directory=True)
    identity = project_scaffolding.capture_source_entry(entry, "linked")
    entry.unlink()
    entry.symlink_to(replacement, target_is_directory=True)
    real_rename = source_entry_ownership.atomic_rename_noreplace
    calls = 0

    def _race_restore(src: Path, dst: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            entry.symlink_to(newcomer, target_is_directory=True)
        real_rename(src, dst)

    monkeypatch.setattr(
        source_entry_ownership,
        "atomic_rename_noreplace",
        _race_restore,
    )

    with pytest.raises(RuntimeError, match="quarantined"):
        project_scaffolding.remove_owned_source_entry(identity)

    assert entry.is_symlink()
    assert entry.resolve() == newcomer.resolve()
    claims = list(source.glob(".app.corsia-claim-*"))
    assert len(claims) == 1
    assert claims[0].is_symlink()
    assert claims[0].resolve() == replacement.resolve()


@pytest.mark.skipif(
    sys.platform not in {"darwin", "linux"},
    reason="atomic no-replace publication is fail-closed on other platforms",
)
def test_atomic_rename_noreplace_moves_directories_without_clobbering(
    tmp_path,
):
    from api.project_scaffolding import atomic_rename_noreplace

    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "owned.txt").write_text("owned\n", encoding="utf-8")
    (destination / "replacement.txt").write_text(
        "replacement\n",
        encoding="utf-8",
    )

    with pytest.raises(FileExistsError):
        atomic_rename_noreplace(source, destination)

    assert (source / "owned.txt").read_text(encoding="utf-8") == "owned\n"
    assert (destination / "replacement.txt").read_text(
        encoding="utf-8"
    ) == "replacement\n"

    moved = tmp_path / "moved"
    atomic_rename_noreplace(source, moved)
    assert not source.exists()
    assert (moved / "owned.txt").read_text(encoding="utf-8") == "owned\n"


@pytest.mark.asyncio
async def test_update_detects_policy_change_before_commit(
    monkeypatch,
    tmp_path,
):
    from api import project_repos
    from fastapi import HTTPException

    workspace = tmp_path / "project"
    source = workspace / "source"
    source.mkdir(parents=True)
    repo = tmp_path / "repo"
    _git_repo_with_commit(repo)
    (source / "app").symlink_to(repo, target_is_directory=True)
    db = _policy_db()
    _insert_policy(db, repo)

    def _concurrent_policy_change(_repo_path):
        db.execute(
            "UPDATE project_repo_policies SET protect_base_checkout = 0 "
            "WHERE project_id = 'p1' AND repo_name = 'app'"
        )
        db.commit()
        return None

    _configure_repo_api(monkeypatch, project_repos, workspace, db)
    monkeypatch.setattr(
        project_repos,
        "branch_pin_support_error",
        _concurrent_policy_change,
    )

    with pytest.raises(HTTPException) as raised:
        await project_repos.update_repo(
            "token",
            "p1",
            "app",
            project_repos.RepoUpdate(pinned_branch="main"),
        )

    assert raised.value.status_code == 409
    assert db.execute(
        "SELECT protect_base_checkout FROM project_repo_policies"
    ).fetchone()[0] == 0
    db.close()


def test_workspace_dir_uses_persisted_project_path_after_rename(
    monkeypatch, tmp_path
):
    from api import project_repos

    project_dir = tmp_path / "original-name"
    project_dir.mkdir()
    calls = []

    class Db:
        def close(self):
            calls.append("closed")

    db = Db()
    monkeypatch.setattr(project_repos, "get_db", lambda: db)

    def resolve(workspace, project_id, *, db):
        calls.append((workspace, project_id, db))
        return "Renamed Project", project_dir.resolve()

    monkeypatch.setattr(
        project_repos,
        "resolve_project_paths",
        resolve,
    )

    assert project_repos._workspace_dir_for("p1") == project_dir.resolve()
    assert calls == [(project_repos.config.WORKSPACE, "p1", db), "closed"]


def test_link_list_detach_roundtrip(tmp_path, home_is_tmp):
    ws = tmp_path / "proj"
    ws.mkdir()
    ext = tmp_path / "ext-repo"
    _init_git_repo(ext)

    row = link_repo_into_source(ws, ext)
    assert row["kind"] == "linked" and row["name"] == "ext-repo"
    assert row["git"] == {"is_repo": True, "branch": "main"}

    repos = list_source_repos(ws)
    assert [r["name"] for r in repos] == ["ext-repo"]
    assert repos[0]["path"] == str(ext.resolve())

    remove_linked_repo(ws, "ext-repo")
    assert list_source_repos(ws) == []
    assert ext.is_dir(), "detach must never touch the target"


def test_link_dotdir_basename_sanitizes(tmp_path, home_is_tmp):
    ws = tmp_path / "proj"; ws.mkdir()
    dot = tmp_path / ".corsia"; dot.mkdir()
    row = link_repo_into_source(ws, dot)
    assert row["name"] == "corsia"


def test_link_rejects_missing_collision_and_self(tmp_path, home_is_tmp):
    ws = tmp_path / "proj"; ws.mkdir()
    with pytest.raises(ValueError, match="Not a directory"):
        link_repo_into_source(ws, tmp_path / "nope")

    ext = tmp_path / "repo"; ext.mkdir()
    link_repo_into_source(ws, ext)
    with pytest.raises(ValueError, match="already exists"):
        link_repo_into_source(ws, ext, name="repo")

    with pytest.raises(ValueError, match="inside this project's source/"):
        link_repo_into_source(ws, ws / "source" / "repo")


def test_link_helper_does_not_impose_repository_policy(
    tmp_path, home_is_tmp
):
    ws = tmp_path / "proj"
    ws.mkdir()
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").write_text("gitdir: /tmp/shared.git/worktrees/linked\n")
    reftable = tmp_path / "reftable"
    (reftable / ".git" / "reftable").mkdir(parents=True)
    (reftable / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (reftable / ".git" / "config").write_text(
        "[extensions]\n\trefStorage = reftable\n"
    )

    linked_row = link_repo_into_source(ws, linked)
    reftable_row = link_repo_into_source(ws, reftable)

    assert linked_row["git"]["is_repo"] is True
    assert reftable_row["git"]["is_repo"] is True
    assert [repo["name"] for repo in list_source_repos(ws)] == [
        "linked",
        "reftable",
    ]


def test_create_native_repo_and_no_detach(tmp_path, monkeypatch):
    ws = tmp_path / "proj"; ws.mkdir()
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "init.defaultRefFormat")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "reftable")
    row = create_repo_in_source(ws, "newrepo")
    assert row["kind"] == "native" and row["git"]["is_repo"] is True
    assert row["git"]["branch"] == "main"
    git_dir = ws / "source" / "newrepo" / ".git"
    storage = subprocess.run(
        [
            "git",
            "config",
            "--file",
            str(git_dir / "config"),
            "--get-all",
            "extensions.refStorage",
        ],
        capture_output=True,
        text=True,
    )
    assert storage.returncode in (0, 1)
    assert storage.stdout.strip().lower() in ("", "files")
    assert not (git_dir / "reftable").exists()

    with pytest.raises(ValueError, match="Native repos"):
        remove_linked_repo(ws, "newrepo")


def test_broken_symlink_flagged_missing(tmp_path, home_is_tmp):
    ws = tmp_path / "proj"; ws.mkdir()
    ext = tmp_path / "gone"; ext.mkdir()
    link_repo_into_source(ws, ext)
    ext.rmdir()
    repos = list_source_repos(ws)
    assert repos[0]["missing"] is True and repos[0]["git"]["is_repo"] is False


def test_legacy_project_source_is_not_implicitly_migrated(tmp_path, home_is_tmp):
    ws = tmp_path / "proj"; ws.mkdir()
    target = tmp_path / ".corsia"; target.mkdir()
    (ws / "project_source").symlink_to(target, target_is_directory=True)

    assert list_source_repos(ws) == []
    assert not (ws / "source").exists()


def test_doc_roots_mirror_disk(tmp_path, home_is_tmp):
    ws = tmp_path / "proj"; ws.mkdir()
    ext = tmp_path / "lib"; ext.mkdir()
    link_repo_into_source(ws, ext)
    roots = compute_doc_roots(ws)
    assert roots[0]["path"] == str(ws)
    assert roots[1] == {"path": str(ext.resolve()), "purpose": "Source repository (lib)."}


def test_link_outside_home_rejected_at_helper(tmp_path):
    ws = tmp_path / "proj"; ws.mkdir()  # real Path.home — tmp_path is outside it
    target = tmp_path / "outside"; target.mkdir()
    with pytest.raises(ValueError, match="home directory"):
        link_repo_into_source(ws, target)


@pytest.mark.integration
class TestRepoEndpoints:
    def _mk_project(self, client) -> str:
        r = client.post(f"/api/projects?token={_TOKEN}", json={"name": _unique_name("repos")})
        assert r.status_code == 200, r.text
        return r.json()["project"]["id"]

    def test_link_list_detach_and_doc_roots_sync(
        self,
        integration_client,
        tmp_path,
        home_is_tmp,
    ):
        ext = tmp_path / f".corsia-test-repo-{uuid.uuid4().hex[:8]}"
        ext.mkdir()
        try:
            pid = self._mk_project(integration_client)
            add = integration_client.post(
                f"/api/projects/{pid}/repos?token={_TOKEN}",
                json={"mode": "link", "path": str(ext), "name": "extrepo"},
            )
            assert add.status_code == 200, add.text
            assert add.json()["repo"]["kind"] == "linked"

            lst = integration_client.get(f"/api/projects/{pid}/repos?token={_TOKEN}")
            assert [r["name"] for r in lst.json()["repos"]] == ["extrepo"]

            detail = integration_client.get(f"/api/projects/{pid}?token={_TOKEN}").json()
            import json as _json
            roots = _json.loads(detail["doc_roots"])
            assert any(r["path"] == str(ext.resolve()) for r in roots)

            gone = integration_client.delete(f"/api/projects/{pid}/repos/extrepo?token={_TOKEN}")
            assert gone.status_code == 200, gone.text
            roots_after = _json.loads(
                integration_client.get(f"/api/projects/{pid}?token={_TOKEN}").json()["doc_roots"]
            )
            assert not any(r["path"] == str(ext.resolve()) for r in roots_after)
            assert ext.is_dir(), "detach must never touch the target"
        finally:
            ext.rmdir()

    def test_list_does_not_migrate_legacy_project_source(
        self,
        integration_client,
        tmp_path_factory,
    ):
        name = _unique_name("legacy-repos")
        created = integration_client.post(
            f"/api/projects?token={_TOKEN}",
            json={"name": name},
        )
        assert created.status_code == 200, created.text
        project = created.json()["project"]
        workspace = Path(created.json()["workspace_path"])
        legacy_repo = tmp_path_factory.mktemp("legacy_repo")
        (workspace / "project_source").symlink_to(
            legacy_repo,
            target_is_directory=True,
        )

        listed = integration_client.get(
            f"/api/projects/{project['id']}/repos?token={_TOKEN}"
        )

        assert listed.status_code == 200, listed.text
        assert listed.json()["repos"] == []
        assert list((workspace / "source").iterdir()) == []

    def test_create_native_repo_then_detach_409(self, integration_client):
        pid = self._mk_project(integration_client)
        add = integration_client.post(
            f"/api/projects/{pid}/repos?token={_TOKEN}", json={"mode": "create", "name": "inproj"}
        )
        assert add.status_code == 200, add.text
        assert add.json()["repo"]["git"]["is_repo"] is True

        refuse = integration_client.delete(f"/api/projects/{pid}/repos/inproj?token={_TOKEN}")
        assert refuse.status_code == 409, refuse.text

    def test_branch_pin_and_checkout_protection_are_independent(
        self,
        integration_client,
        tmp_path,
        home_is_tmp,
    ):
        ext = tmp_path / f".corsia-test-repo-{uuid.uuid4().hex[:8]}"
        _init_git_repo(ext)
        try:
            pid = self._mk_project(integration_client)
            added = integration_client.post(
                f"/api/projects/{pid}/repos?token={_TOKEN}",
                json={"mode": "link", "path": str(ext), "name": "app"},
            )
            assert added.status_code == 200, added.text
            assert added.json()["repo"]["pinned_branch"] == "main"
            assert added.json()["repo"]["protect_base_checkout"] is True
            assert added.json()["repo"]["branch_pinned"] is True
            catalog = integration_client.get(
                f"/api/projects/{pid}/repos/app/branches?token={_TOKEN}"
            )
            assert catalog.status_code == 200, catalog.text
            assert catalog.json() == {
                "current_branch": "main",
                "branches": [
                    {"name": "main", "source": "local", "remote": None}
                ],
            }

            unprotected = integration_client.patch(
                f"/api/projects/{pid}/repos/app?token={_TOKEN}",
                json={"protect_base_checkout": False},
            )
            assert unprotected.status_code == 200, unprotected.text
            assert unprotected.json()["repo"]["pinned_branch"] == "main"
            assert unprotected.json()["repo"]["protect_base_checkout"] is False

            unpinned = integration_client.patch(
                f"/api/projects/{pid}/repos/app?token={_TOKEN}",
                json={"pinned_branch": None},
            )
            assert unpinned.status_code == 200, unpinned.text
            assert unpinned.json()["repo"]["pinned_branch"] is None
            assert unpinned.json()["repo"]["protect_base_checkout"] is False
            assert unpinned.json()["repo"]["branch_pinned"] is False
            listed = integration_client.get(
                f"/api/projects/{pid}/repos?token={_TOKEN}"
            )
            assert listed.json()["repos"][0]["branch_pinned"] is False

            protected = integration_client.patch(
                f"/api/projects/{pid}/repos/app?token={_TOKEN}",
                json={"protect_base_checkout": True},
            )
            assert protected.status_code == 200, protected.text
            assert protected.json()["repo"]["pinned_branch"] is None
            assert protected.json()["repo"]["protect_base_checkout"] is True

            repinned = integration_client.patch(
                f"/api/projects/{pid}/repos/app?token={_TOKEN}",
                json={"pinned_branch": "main"},
            )
            assert repinned.status_code == 200, repinned.text
            assert repinned.json()["repo"]["pinned_branch"] == "main"
            assert repinned.json()["repo"]["protect_base_checkout"] is True
            assert repinned.json()["repo"]["branch_pinned"] is True
        finally:
            if ext.exists():
                for child in sorted(ext.rglob("*"), reverse=True):
                    child.unlink() if child.is_file() else child.rmdir()
                ext.rmdir()

    def test_detach_clears_unpin_override(
        self,
        integration_client,
        tmp_path,
        home_is_tmp,
    ):
        ext = tmp_path / f".corsia-test-repo-{uuid.uuid4().hex[:8]}"
        _init_git_repo(ext)
        try:
            pid = self._mk_project(integration_client)
            endpoint = f"/api/projects/{pid}/repos/app?token={_TOKEN}"
            integration_client.post(
                f"/api/projects/{pid}/repos?token={_TOKEN}",
                json={"mode": "link", "path": str(ext), "name": "app"},
            )
            assert integration_client.patch(
                endpoint,
                json={
                    "pinned_branch": None,
                    "protect_base_checkout": False,
                },
            ).status_code == 200
            assert integration_client.delete(endpoint).status_code == 200

            readded = integration_client.post(
                f"/api/projects/{pid}/repos?token={_TOKEN}",
                json={"mode": "link", "path": str(ext), "name": "app"},
            )
            assert readded.status_code == 200, readded.text
            assert readded.json()["repo"]["branch_pinned"] is True
        finally:
            if ext.exists():
                for child in sorted(ext.rglob("*"), reverse=True):
                    child.unlink() if child.is_file() else child.rmdir()
                ext.rmdir()

    def test_link_path_outside_home_rejected(self, integration_client):
        pid = self._mk_project(integration_client)
        bad = integration_client.post(
            f"/api/projects/{pid}/repos?token={_TOKEN}", json={"mode": "link", "path": "/tmp"}
        )
        assert bad.status_code == 400, bad.text

    def test_unknown_project_404(self, integration_client):
        r = integration_client.get(f"/api/projects/nope-xyz/repos?token={_TOKEN}")
        assert r.status_code == 404


@pytest.mark.integration
class TestFsBrowse:
    def test_browse_home_default_and_gate(self, integration_client):
        r = integration_client.get(f"/api/fs/browse?token={_TOKEN}")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["parent"] is None, "home is the root of the picker"
        assert all(not d["name"].startswith(".") for d in data["dirs"])

        outside = integration_client.get(f"/api/fs/browse?token={_TOKEN}&path=/etc")
        assert outside.status_code == 400

    def test_browse_marks_symlinked_directories(
        self,
        integration_client,
        tmp_path,
        home_is_tmp,
    ):
        (tmp_path / "real").mkdir()
        (tmp_path / "linked").symlink_to(tmp_path / "real", target_is_directory=True)
        r = integration_client.get(
            f"/api/fs/browse?token={_TOKEN}&path={tmp_path}"
        )
        assert r.status_code == 200, r.text
        links = {d["name"]: d["is_link"] for d in r.json()["dirs"]}
        assert links["real"] is False
        assert links["linked"] is True

    def test_browse_files_mode_with_ext_filter(
        self,
        integration_client,
        tmp_path,
        home_is_tmp,
    ):
        d = tmp_path / f".corsia-test-browse-{uuid.uuid4().hex[:8]}"
        d.mkdir()
        try:
            (d / "a.md").write_text("x")
            (d / "b.txt").write_text("x")
            (d / "c.py").write_text("x")
            r = integration_client.get(
                f"/api/fs/browse?token={_TOKEN}&path={d}&files=1&ext=.md,.txt"
            )
            assert r.status_code == 200, r.text
            names = sorted(f["name"] for f in r.json()["files"])
            assert names == ["a.md", "b.txt"]
        finally:
            for f in d.iterdir():
                f.unlink()
            d.rmdir()
