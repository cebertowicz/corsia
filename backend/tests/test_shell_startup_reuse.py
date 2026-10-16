"""A protected Bash call builds its Safehouse argv from the policy snapshot it already loaded."""

from __future__ import annotations

import shutil
from pathlib import Path

from infra.project_repo_policy import RepoPolicy
from tools import sandbox_config, shell_guard


def test_argv_grants_come_from_the_callers_policy_snapshot(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    protected = tmp_path / "code" / "protected"
    writable = tmp_path / "code" / "writable"
    for repo in (protected, writable):
        repo.mkdir(parents=True)
    policies = [
        RepoPolicy("p1", "protected", protected.resolve(), "main", True),
        RepoPolicy("p1", "writable", writable.resolve(), None, False),
    ]

    def reread(*_args, **_kwargs):
        raise AssertionError("argv grants must not re-read repository policies")

    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [workspace])
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RO", [])
    monkeypatch.setattr(sandbox_config, "_list_authoritative_repo_policies", reread)
    monkeypatch.setattr(sandbox_config, "thread_bind_roots", lambda _thread_id: [])
    monkeypatch.setattr(shell_guard, "_safehouse_executable", lambda: Path("/opt/safehouse"))
    monkeypatch.setattr(shell_guard, "agent_account_ro_dirs", lambda _view: [])

    argv = shell_guard.safehouse_argv(
        "printf ok",
        workspace,
        tmp_path / "envelope.sb",
        policies=policies,
    )

    assert argv[:3] == ["/opt/safehouse", "--workdir=", "--trust-workdir-config=false"]
    assert argv[3] == f"--add-dirs={workspace.resolve()}:{writable.resolve()}"
    assert argv[4] == f"--add-dirs-ro={protected.resolve()}"
    assert argv[5:7] == [
        "--enable=ssh,agent-browser",
        f"--append-profile={(tmp_path / 'envelope.sb').resolve()}",
    ]
    assert argv[-6:] == ["--", "/bin/bash", "--noprofile", "--norc", "-c", "printf ok"]


def test_worktree_listing_keeps_only_reciprocally_linked_directories(tmp_path):
    repo = tmp_path / "repo"
    git_dir = repo / ".git"
    (git_dir / "worktrees" / "linked").mkdir(parents=True)
    linked = repo / ".worktrees" / "linked"
    stray = repo / ".worktrees" / "stray"
    linked.mkdir(parents=True)
    stray.mkdir(parents=True)
    (linked / ".git").write_text(f"gitdir: {git_dir / 'worktrees' / 'linked'}\n", encoding="utf-8")
    (git_dir / "worktrees" / "linked" / "gitdir").write_text(f"{linked / '.git'}\n", encoding="utf-8")

    assert sandbox_config.list_repo_worktrees(repo) == [linked.resolve()]


def _link(repo: Path, name: str) -> Path:
    gitdir = repo / ".git" / "worktrees" / name
    gitdir.mkdir(parents=True, exist_ok=True)
    worktree = repo / ".worktrees" / name
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    (gitdir / "gitdir").write_text(f"{worktree / '.git'}\n", encoding="utf-8")
    return worktree.resolve()


def test_unchanged_worktrees_are_not_reverified(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    linked = [_link(repo, f"w{i}") for i in range(5)]
    assert sandbox_config.list_repo_worktrees(repo) == sorted(linked)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("unchanged worktrees must not be re-verified")

    monkeypatch.setattr(sandbox_config, "verified_worktree_marker", forbidden)
    assert sandbox_config.list_repo_worktrees(repo) == sorted(linked)


def test_added_and_removed_worktrees_show_on_the_next_call(tmp_path):
    repo = tmp_path / "repo"
    first = _link(repo, "first")
    assert sandbox_config.list_repo_worktrees(repo) == [first]

    second = _link(repo, "second")
    assert sandbox_config.list_repo_worktrees(repo) == sorted([first, second])

    shutil.rmtree(first)
    assert sandbox_config.list_repo_worktrees(repo) == [second]


def test_forged_marker_is_denied_on_the_next_call(tmp_path):
    repo = tmp_path / "repo"
    linked = _link(repo, "linked")
    other = _link(repo, "other")
    assert sandbox_config.list_repo_worktrees(repo) == sorted([linked, other])

    # Repoint a cached worktree at another worktree's gitdir: reciprocity fails.
    (linked / ".git").write_text(
        f"gitdir: {repo / '.git' / 'worktrees' / 'other'}\n", encoding="utf-8"
    )
    assert sandbox_config.list_repo_worktrees(repo) == [other]


def test_edited_reciprocal_gitdir_is_denied_on_the_next_call(tmp_path):
    repo = tmp_path / "repo"
    linked = _link(repo, "linked")
    assert sandbox_config.list_repo_worktrees(repo) == [linked]

    reciprocal = repo / ".git" / "worktrees" / "linked" / "gitdir"
    reciprocal.write_text(f"{tmp_path / 'elsewhere' / '.git'}\n", encoding="utf-8")
    assert sandbox_config.list_repo_worktrees(repo) == []


def test_marker_swapped_for_symlink_is_denied_on_the_next_call(tmp_path):
    repo = tmp_path / "repo"
    linked = _link(repo, "linked")
    assert sandbox_config.list_repo_worktrees(repo) == [linked]

    real = tmp_path / "real-marker"
    real.write_text((linked / ".git").read_text(encoding="utf-8"), encoding="utf-8")
    (linked / ".git").unlink()
    (linked / ".git").symlink_to(real)
    assert sandbox_config.list_repo_worktrees(repo) == []


def test_gitdir_outside_git_worktrees_is_denied_on_the_next_call(tmp_path):
    repo = tmp_path / "repo"
    linked = _link(repo, "linked")
    assert sandbox_config.list_repo_worktrees(repo) == [linked]

    outside = tmp_path / "outside-gitdir"
    outside.mkdir()
    (outside / "gitdir").write_text(f"{linked / '.git'}\n", encoding="utf-8")
    (linked / ".git").write_text(f"gitdir: {outside}\n", encoding="utf-8")
    assert sandbox_config.list_repo_worktrees(repo) == []


def test_gitdir_replaced_by_symlink_is_denied_on_the_next_call(tmp_path):
    repo = tmp_path / "repo"
    linked = _link(repo, "linked")
    assert sandbox_config.list_repo_worktrees(repo) == [linked]

    gitdir = repo / ".git" / "worktrees" / "linked"
    moved = tmp_path / "moved-gitdir"
    gitdir.rename(moved)
    gitdir.symlink_to(moved)
    assert sandbox_config.list_repo_worktrees(repo) == []


def test_late_marker_on_a_listed_directory_is_picked_up(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".worktrees" / "pending").mkdir(parents=True)
    assert sandbox_config.list_repo_worktrees(repo) == []

    linked = _link(repo, "pending")
    assert sandbox_config.list_repo_worktrees(repo) == [linked]
