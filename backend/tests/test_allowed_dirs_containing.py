"""A per-path RW lookup grants exactly what the full list grants, without its cost."""

from pathlib import Path

import pytest

from infra.project_repo_policy import RepoPolicy
from tests.project_ref_helpers import project_ref, select_workspace
from tests.test_repo_branch_pinning import _git_repo, _policy_db
from tests.test_sandbox_config import _configure_sandbox, _linked_worktree


def _sandbox(tmp_path: Path, monkeypatch):
    from tools import sandbox_config

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repos = {
        name: _git_repo(tmp_path / name, "dev").resolve()
        for name in ("protected", "quarantined", "writable")
    }
    worktrees = [_linked_worktree(repos["protected"], name) for name in ("a", "b")]
    (repos["protected"] / ".worktrees" / "rogue").mkdir()
    quarantined = _linked_worktree(repos["quarantined"], "q")
    policies = [
        RepoPolicy("p1", "protected", repos["protected"], "dev", True),
        RepoPolicy(
            "p1", "quarantined", repos["quarantined"], "dev", True,
            enforcement_error="stale binding",
        ),
        RepoPolicy("p1", "writable", repos["writable"], None, False),
    ]
    _configure_sandbox(
        monkeypatch, sandbox_config, workspace, lambda **_kwargs: policies,
    )
    probes = [
        workspace / "shared" / "doc.md",
        repos["protected"] / "base.py",
        *(worktree / "src" / "mod.py" for worktree in worktrees),
        repos["protected"] / ".worktrees" / "rogue" / "x.py",
        quarantined / "x.py",
        repos["writable"] / "x.py",
        tmp_path / "elsewhere" / "x.py",
    ]
    return sandbox_config, probes


def test_containing_matches_the_full_list_for_every_zone(tmp_path, monkeypatch):
    sandbox_config, probes = _sandbox(tmp_path, monkeypatch)
    full = sandbox_config.get_allowed_dirs_rw()

    for probe in probes:
        expected = {root for root in full if probe.resolve().is_relative_to(root.resolve())}
        assert set(sandbox_config.get_allowed_dirs_rw(containing=probe)) == expected, probe


def test_file_batch_resolution_uses_each_path_containing_scope(monkeypatch, tmp_path):
    from registry import file_resolution

    paths = [
        str(tmp_path / "one" / "doc.md"),
        str(tmp_path / "one" / "second.md"),
        str(tmp_path / "two" / "doc.md"),
    ]
    calls: list[Path] = []

    def allowed_dirs(*, db=None, containing=None, policies=None):
        calls.append(containing)
        return []

    monkeypatch.setattr(file_resolution, "get_allowed_dirs_rw", allowed_dirs)
    monkeypatch.setattr(file_resolution, "prepare_file_identity", lambda *_args, **_kwargs: None)

    assert file_resolution.resolve_file_identities(paths) == {}
    assert calls == [Path(paths[0]).parent, Path(paths[2]).parent]


def test_containing_never_lists_worktrees(tmp_path, monkeypatch):
    sandbox_config, probes = _sandbox(tmp_path, monkeypatch)

    def _no_listing(_repo):
        raise AssertionError("a per-path lookup must not list every worktree")

    monkeypatch.setattr(sandbox_config, "list_repo_worktrees", _no_listing)
    worktree_file = probes[2]
    assert sandbox_config.get_allowed_dirs_rw(containing=worktree_file) == [
        worktree_file.parents[1],
    ]


@pytest.fixture
def two_pinned_repos(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    select_workspace(monkeypatch, workspace)
    project = workspace / "projects" / "demo"
    (project / "source").mkdir(parents=True)
    db = _policy_db()
    db.execute(
        "INSERT INTO projects (id, name, path) VALUES ('p1', 'Demo', ?)",
        (project_ref("demo"),),
    )
    repos = {}
    for name in ("app", "lib"):
        repos[name] = _git_repo(tmp_path / name).resolve()
        (project / "source" / name).symlink_to(repos[name], target_is_directory=True)
        db.execute(
            "INSERT INTO project_repo_policies VALUES ('p1', ?, ?, 'main', 1)",
            (name, str(repos[name])),
        )
    return workspace, db, repos


def test_containing_verifies_only_the_repository_holding_the_path(
    two_pinned_repos, monkeypatch,
):
    from infra import project_repo_policy
    from infra.git_state_cache import clear_git_state_cache

    workspace, db, repos = two_pinned_repos
    clear_git_state_cache()  # this test counts Git branch reads; a cache hit reads none
    checked: list[Path] = []
    real_branch = project_repo_policy.current_repo_branch
    monkeypatch.setattr(
        project_repo_policy, "current_repo_branch",
        lambda path: checked.append(path) or real_branch(path),
    )

    everything = project_repo_policy.list_registered_repo_policies(workspace=workspace, db=db)
    assert sorted(checked) == sorted(repos.values())
    checked.clear()
    clear_git_state_cache()

    held = project_repo_policy.list_registered_repo_policies(
        workspace=workspace, db=db, containing=repos["app"] / "src" / "x.py",
    )
    assert held == [policy for policy in everything if policy.repo_name == "app"]
    assert checked == [repos["app"]]
    checked.clear()
    clear_git_state_cache()

    outside = project_repo_policy.list_registered_repo_policies(
        workspace=workspace, db=db, containing=workspace / "shared" / "doc.md",
    )
    assert outside == [] and checked == []
