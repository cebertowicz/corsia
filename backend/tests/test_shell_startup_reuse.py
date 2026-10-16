"""A protected Bash call builds its Safehouse argv from the policy snapshot it already loaded."""

from __future__ import annotations

from pathlib import Path

from infra.project_repo_policy import RepoPolicy
from tools import sandbox_config, sandbox_git, shell_guard


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


def test_listing_keeps_symlink_entry_and_parent_semantics(tmp_path):
    repo = tmp_path / "repo"
    first, second = _link(repo, "a"), _link(repo, "b")
    (repo / ".worktrees" / "stray").mkdir()
    (repo / ".worktrees" / "alias").symlink_to(first)
    (repo / ".worktrees" / "dangling").symlink_to(tmp_path / "missing")

    # A symlink to a sibling worktree lists that sibling's root once more.
    assert sandbox_config.list_repo_worktrees(repo) == sorted([first, first, second])

    redirected = tmp_path / "redirected"
    (redirected / ".git").mkdir(parents=True)
    (redirected / ".worktrees").symlink_to(repo / ".worktrees")
    assert sandbox_config.list_repo_worktrees(redirected) == []


def test_profile_worktree_rules_match_resolved_rendering(tmp_path):
    plain = tmp_path / "plain"
    linked = [_link(plain, "a"), _link(plain, "b")]
    (plain / ".worktrees" / "alias").symlink_to(linked[0])
    behind_link = tmp_path / "behind-link"
    _link(behind_link, "hidden")
    redirected = tmp_path / "redirected"
    (redirected / ".git").mkdir(parents=True)
    (redirected / ".worktrees").symlink_to(behind_link / ".worktrees")
    alias_root = tmp_path / "alias-root"
    alias_root.symlink_to(plain)
    policies = [
        RepoPolicy("p1", "plain", alias_root, "main", True),
        RepoPolicy("p1", "redirected", redirected.resolve(), "main", True),
    ]

    profile = shell_guard.pinned_repo_sandbox_profile(
        "printf ok", str(tmp_path), policies=policies
    )

    # The pre-change rendering resolved every worktree path and its .git marker.
    for root in linked:
        marker = root / ".git"
        allow = shell_guard._sandbox_path(root)
        deny = shell_guard._sandbox_path(marker)
        assert f'(allow file-read* file-write* (subpath "{allow}"))' in profile
        assert f'(deny file-write* (literal "{deny}"))' in profile
        assert sandbox_git.sandbox_literal(root) == allow
        assert sandbox_git.sandbox_literal(marker) == deny
    assert profile.count("(allow file-read* file-write* (subpath") == len(
        sandbox_config.list_repo_worktrees(plain)
    )
    assert "hidden" not in profile
