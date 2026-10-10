from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from infra.project_repo_policy import RepoPolicy
from tools.worktrees import WorktreeCreateTool


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "init.defaultRefFormat=files", *args],
        cwd=str(repo),
        check=check,
        capture_output=True,
        text=True,
    )


def _commit(repo: Path, message: str, content: str) -> str:
    (repo / "tracked.txt").write_text(content, encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(
        repo,
        "-c",
        "user.name=CORSIA Test",
        "-c",
        "user.email=test@corsia.local",
        "commit",
        "-m",
        message,
    )
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _stale_clone(tmp_path: Path) -> tuple[Path, Path, str, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    author = tmp_path / "author"
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"

    _git(tmp_path, "init", "-b", "dev", str(author))
    first = _commit(author, "first", "first\n")
    _git(tmp_path, "clone", "--bare", str(author), str(remote))
    _git(tmp_path, "clone", "--branch", "dev", str(remote), str(repo))
    _git(author, "remote", "add", "origin", str(remote))

    second = _commit(author, "second", "second\n")
    _git(author, "push", "origin", "dev")

    assert _git(repo, "rev-parse", "dev").stdout.strip() == first
    assert _git(repo, "rev-parse", "origin/dev").stdout.strip() == first
    return repo, remote, first, second


@pytest.mark.asyncio
@pytest.mark.integration
async def test_worktree_create_fetches_before_resolving_remote_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _remote, stale_local, remote_head = _stale_clone(tmp_path)
    policy = RepoPolicy("p1", "app", repo.resolve(), "dev", True)
    monkeypatch.setattr(
        "tools.worktrees.list_registered_repo_policies",
        lambda **_kwargs: [policy],
    )
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "tools.worktrees.emit_product_event",
        lambda category, action, **_kwargs: events.append((category, action)),
    )

    result = await WorktreeCreateTool().execute(
        repo_name="app",
        branch="fix/fresh-base",
        descriptor="fresh-base",
        remote="origin",
        base_branch="dev",
        _thread_id="thread-1",
        _thread_project_id="p1",
    )

    receipt = json.loads(result)
    worktree = Path(receipt["worktree_path"])
    assert receipt["base_sha"] == remote_head
    assert receipt["base_sha"] != stale_local
    assert _git(worktree, "rev-parse", "HEAD").stdout.strip() == remote_head
    assert _git(repo, "rev-parse", "dev").stdout.strip() == stale_local
    assert events == [("worktree", "created")]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_worktree_create_fetch_failure_creates_no_branch_or_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _remote, _stale_local, _remote_head = _stale_clone(tmp_path)
    _git(repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    policy = RepoPolicy("p1", "app", repo.resolve(), "dev", True)
    monkeypatch.setattr(
        "tools.worktrees.list_registered_repo_policies",
        lambda **_kwargs: [policy],
    )
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "tools.worktrees.emit_product_event",
        lambda category, action, **_kwargs: events.append((category, action)),
    )

    result = await WorktreeCreateTool().execute(
        repo_name="app",
        branch="fix/fetch-failure",
        descriptor="fetch-failure",
        remote="origin",
        base_branch="dev",
        _thread_id="thread-1",
        _thread_project_id="p1",
    )

    assert result.startswith("Error:")
    assert "fetch" in result.lower()
    assert events == [("worktree", "create_failed")]
    assert not (repo / ".worktrees" / "thread-1-fetch-failure").exists()
    assert (
        _git(
            repo,
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/fix/fetch-failure",
            check=False,
        ).returncode
        != 0
    )


@pytest.mark.asyncio
async def test_worktree_create_rejects_invalid_scope_and_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _remote, _stale_local, _remote_head = _stale_clone(tmp_path)
    policy = RepoPolicy("other-project", "app", repo.resolve(), "dev", True)

    def _foreign_policies(*, project_id: str | None = None):
        return [policy] if project_id == "other-project" else []

    monkeypatch.setattr(
        "tools.worktrees.list_registered_repo_policies",
        _foreign_policies,
    )
    tool = WorktreeCreateTool()

    unowned = await tool.execute(
        repo_name="app",
        branch="fix/nope",
        descriptor="safe",
        _thread_id="thread-1",
        _thread_project_id="p1",
    )
    unsafe = await tool.execute(
        repo_name="app",
        branch="fix/nope",
        descriptor="../escape",
        _thread_id="thread-1",
        _thread_project_id="other-project",
    )

    pinned_policy = RepoPolicy("p1", "app", repo.resolve(), "dev", True)
    monkeypatch.setattr(
        "tools.worktrees.list_registered_repo_policies",
        lambda **_kwargs: [pinned_policy],
    )
    wrong_base = await tool.execute(
        repo_name="app",
        branch="fix/nope",
        descriptor="safe",
        base_branch="main",
        _thread_id="thread-1",
        _thread_project_id="p1",
    )

    assert unowned.startswith("Error:")
    assert "no repositories are registered" in unowned
    assert unsafe.startswith("Error:")
    assert "descriptor" in unsafe
    assert wrong_base.startswith("Error:")
    assert "pinned branch 'dev'" in wrong_base


def _stale_clone_pair(tmp_path: Path) -> tuple[Path, Path]:
    """Return two independent stale clones named ``app`` and ``tools``."""
    app, _remote, _stale_local, _remote_head = _stale_clone(tmp_path / "app-tree")
    tools, _remote2, _stale_local2, _remote_head2 = _stale_clone(
        tmp_path / "tools-tree"
    )
    return app, tools


@pytest.mark.asyncio
async def test_worktree_create_selects_either_repo_in_a_multi_repo_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, tools = _stale_clone_pair(tmp_path)
    app_policy = RepoPolicy("p1", "app", app.resolve(), "dev", True)
    tools_policy = RepoPolicy("p1", "tools", tools.resolve(), "dev", False)
    monkeypatch.setattr(
        "tools.worktrees.list_registered_repo_policies",
        lambda **_kwargs: [app_policy, tools_policy],
    )
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "tools.worktrees.emit_product_event",
        lambda category, action, **_kwargs: events.append((category, action)),
    )

    result_a = await WorktreeCreateTool().execute(
        repo_name="app",
        branch="fix/repo-a",
        descriptor="repo-a",
        remote="origin",
        _thread_id="thread-1",
        _thread_project_id="p1",
    )
    receipt_a = json.loads(result_a)
    assert receipt_a["repo_name"] == "app"
    assert Path(receipt_a["worktree_path"]).is_relative_to(app.resolve())

    result_t = await WorktreeCreateTool().execute(
        repo_name="tools",
        branch="fix/repo-tools",
        descriptor="repo-tools",
        remote="origin",
        base_branch="dev",
        _thread_id="thread-1",
        _thread_project_id="p1",
    )
    receipt_t = json.loads(result_t)
    assert receipt_t["repo_name"] == "tools"
    assert Path(receipt_t["worktree_path"]).is_relative_to(tools.resolve())

    assert len(events) == 2
    assert all(action == "created" for _category, action in events)
    # Each repo owns its own .worktrees root and its own new branch.
    assert (app / ".worktrees" / "thread-1-repo-a").is_dir()
    assert (tools / ".worktrees" / "thread-1-repo-tools").is_dir()
    assert not (app / ".worktrees" / "thread-1-repo-tools").exists()
    assert not (tools / ".worktrees" / "thread-1-repo-a").exists()


@pytest.mark.asyncio
async def test_worktree_create_uses_project_scoped_policy_before_coalescing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _remote, _stale_local, _remote_head = _stale_clone(tmp_path)
    other_policy = RepoPolicy("other-project", "shared", app.resolve(), "dev", True)
    project_policy = RepoPolicy("p1", "app", app.resolve(), "dev", True)
    calls: list[str | None] = []

    def _policies(*, project_id: str | None = None):
        calls.append(project_id)
        if project_id == "p1":
            return [project_policy]
        return [other_policy]

    monkeypatch.setattr("tools.worktrees.list_registered_repo_policies", _policies)
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "tools.worktrees.emit_product_event",
        lambda category, action, **_kwargs: events.append((category, action)),
    )

    result = await WorktreeCreateTool().execute(
        repo_name="app",
        branch="fix/project-scope",
        descriptor="project-scope",
        _thread_id="thread-1",
        _thread_project_id="p1",
    )

    receipt = json.loads(result)
    assert receipt["repo_name"] == "app"
    assert calls == ["p1"]
    assert events == [("worktree", "created")]


@pytest.mark.asyncio
async def test_worktree_create_unknown_repo_lists_valid_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, tools = _stale_clone_pair(tmp_path)
    app_policy = RepoPolicy("p1", "app", app.resolve(), "dev", True)
    tools_policy = RepoPolicy("p1", "tools", tools.resolve(), "dev", False)
    monkeypatch.setattr(
        "tools.worktrees.list_registered_repo_policies",
        lambda **_kwargs: [app_policy, tools_policy],
    )
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "tools.worktrees.emit_product_event",
        lambda category, action, **_kwargs: events.append((category, action)),
    )

    result = await WorktreeCreateTool().execute(
        repo_name="missing",
        branch="fix/nope",
        descriptor="safe",
        _thread_id="thread-1",
        _thread_project_id="p1",
    )

    assert result.startswith("Error:")
    assert "registered repository 'missing' is unavailable" in result
    assert "valid repositories: app, tools" in result
    assert events == [("worktree", "create_failed")]
    assert not (app / ".worktrees" / "thread-1-safe").exists()
    assert not (tools / ".worktrees" / "thread-1-safe").exists()


@pytest.mark.asyncio
async def test_worktree_create_policy_isolation_between_repos(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, tools = _stale_clone_pair(tmp_path)
    # app is pinned to dev; tools is unpinned.
    app_policy = RepoPolicy("p1", "app", app.resolve(), "dev", True)
    tools_policy = RepoPolicy("p1", "tools", tools.resolve(), None, False)
    monkeypatch.setattr(
        "tools.worktrees.list_registered_repo_policies",
        lambda **_kwargs: [app_policy, tools_policy],
    )
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "tools.worktrees.emit_product_event",
        lambda category, action, **_kwargs: events.append((category, action)),
    )

    # Unpinned repo requires base_branch.
    missing_base = await WorktreeCreateTool().execute(
        repo_name="tools",
        branch="fix/tools-base",
        descriptor="tools-base",
        _thread_id="thread-1",
        _thread_project_id="p1",
    )
    assert missing_base.startswith("Error:")
    assert "base_branch is required" in missing_base
    assert events == [("worktree", "create_failed")]

    # Pinned repo rejects a base_branch that differs from its pin.
    wrong_pin = await WorktreeCreateTool().execute(
        repo_name="app",
        branch="fix/app-pin",
        descriptor="app-pin",
        base_branch="main",
        _thread_id="thread-1",
        _thread_project_id="p1",
    )
    assert wrong_pin.startswith("Error:")
    assert "pinned branch 'dev'" in wrong_pin
    assert len(events) == 2

    # Both create successfully when each honors its own policy.
    ok_app = json.loads(
        await WorktreeCreateTool().execute(
            repo_name="app",
            branch="fix/app-ok",
            descriptor="app-ok",
            _thread_id="thread-1",
            _thread_project_id="p1",
        )
    )
    ok_tools = json.loads(
        await WorktreeCreateTool().execute(
            repo_name="tools",
            branch="fix/tools-ok",
            descriptor="tools-ok",
            base_branch="dev",
            _thread_id="thread-1",
            _thread_project_id="p1",
        )
    )
    assert ok_app["repo_name"] == "app"
    assert ok_tools["repo_name"] == "tools"
    assert len(events) == 4


@pytest.mark.asyncio
async def test_worktree_create_no_registered_repos_gives_next_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy single-repo project with no policies gets a next step."""
    monkeypatch.setattr(
        "tools.worktrees.list_registered_repo_policies",
        lambda **_kwargs: [],
    )
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "tools.worktrees.emit_product_event",
        lambda category, action, **_kwargs: events.append((category, action)),
    )

    result = await WorktreeCreateTool().execute(
        repo_name="app",
        branch="fix/nope",
        descriptor="safe",
        _thread_id="thread-1",
        _thread_project_id="p1",
    )

    assert result.startswith("Error:")
    assert "no repositories are registered" in result
    assert "project_repo_register" in result
    assert events == [("worktree", "create_failed")]
