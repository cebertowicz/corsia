from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

from infra.project_repo_policy import RepoPolicy
from tools import shell_guard


def _git_repo(path: Path) -> Path:
    subprocess.run(
        [
            "/usr/bin/git",
            "-c",
            "init.defaultRefFormat=files",
            "init",
            "-b",
            "main",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    tracked = path / "tracked.txt"
    tracked.write_text("base\n", encoding="utf-8")
    subprocess.run(
        ["/usr/bin/git", "-C", str(path), "add", "tracked.txt"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(path),
            "-c",
            "user.name=CORSIA Test",
            "-c",
            "user.email=test@corsia.local",
            "commit",
            "-m",
            "initial",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return path


def _linked_worktree(repo: Path) -> Path:
    worktree = repo / ".worktrees" / "feature"
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(repo),
            "worktree",
            "add",
            "-b",
            "feature",
            str(worktree),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return worktree


def _policy(
    repo: Path,
    *,
    project_id: str = "project",
    repo_name: str = "repo",
    enforcement_error: str | None = None,
    source_binding_valid: bool = True,
) -> RepoPolicy:
    return RepoPolicy(
        project_id,
        repo_name,
        repo.resolve(),
        "main",
        True,
        enforcement_error=enforcement_error,
        source_binding_valid=source_binding_valid,
    )


def _git_allow(repo: Path) -> str:
    return f'(allow file-read* file-write* (subpath "{repo / ".git"}"))'


def test_verified_worktree_git_grant_is_independent_of_working_dir(
    tmp_path,
    monkeypatch,
) -> None:
    repo = _git_repo(tmp_path / "repo")
    worktree = _linked_worktree(repo)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [_policy(repo)],
    )

    grant_policies = [_policy(repo)]
    inside_profile = shell_guard.pinned_repo_sandbox_profile(
        "git status --short && true",
        str(worktree),
        grant_policies=grant_policies,
    )
    outside_profile = shell_guard.pinned_repo_sandbox_profile(
        f"cd {shlex.quote(str(worktree))} && git status --short",
        str(outside),
        grant_policies=grant_policies,
    )

    assert _git_allow(repo) in inside_profile
    assert _git_allow(repo) in outside_profile


def test_nonliteral_git_grant_is_limited_to_invoking_project(
    tmp_path,
) -> None:
    owned_repo = _git_repo(tmp_path / "owned-repo")
    other_repo = _git_repo(tmp_path / "other-repo")
    _linked_worktree(owned_repo)
    _linked_worktree(other_repo)
    outside = tmp_path / "outside"
    outside.mkdir()

    owned_policy = _policy(
        owned_repo,
        project_id="owned-project",
        repo_name="owned-repo",
    )
    profile = shell_guard.pinned_repo_sandbox_profile(
        "pytest",
        str(outside),
        grant_policies=[owned_policy],
        policies=[
            owned_policy,
            _policy(
                other_repo,
                project_id="other-project",
                repo_name="other-repo",
            ),
        ],
    )

    assert _git_allow(owned_repo) in profile
    assert _git_allow(other_repo) not in profile
    assert f'(deny file-write* (subpath "{other_repo.resolve()}"))' in profile


def test_project_scoped_grant_survives_global_shared_checkout_coalescing(
    tmp_path,
) -> None:
    repo = _git_repo(tmp_path / "shared-repo")
    _linked_worktree(repo)
    global_policy = _policy(
        repo,
        project_id="first-project",
        repo_name="shared-repo",
    )
    scoped_policy = _policy(
        repo,
        project_id="invoking-project",
        repo_name="shared-repo",
    )

    profile = shell_guard.pinned_repo_sandbox_profile(
        "pytest",
        str(tmp_path),
        grant_policies=[scoped_policy],
        policies=[global_policy],
    )

    assert _git_allow(repo) in profile


@pytest.mark.parametrize("grant_policies", [None, []])
def test_nonliteral_git_grant_fails_closed_without_scoped_snapshot(
    grant_policies,
    tmp_path,
) -> None:
    repo = _git_repo(tmp_path / "repo")
    _linked_worktree(repo)

    profile = shell_guard.pinned_repo_sandbox_profile(
        "pytest",
        str(tmp_path),
        grant_policies=grant_policies,
        policies=[_policy(repo)],
    )

    assert _git_allow(repo) not in profile
    assert f'(deny file-write* (subpath "{repo.resolve()}"))' in profile


def test_repo_without_verified_worktree_gets_no_ordinary_git_grant(
    tmp_path,
    monkeypatch,
) -> None:
    repo = _git_repo(tmp_path / "repo")
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [_policy(repo)],
    )

    profile = shell_guard.pinned_repo_sandbox_profile(
        "pytest",
        str(outside),
        grant_policies=[_policy(repo)],
    )

    assert _git_allow(repo) not in profile


@pytest.mark.parametrize("broken_side", ["marker", "reciprocal"])
def test_nonreciprocal_worktree_gets_no_ordinary_git_grant(
    broken_side,
    tmp_path,
    monkeypatch,
) -> None:
    repo = _git_repo(tmp_path / "repo")
    worktree = _linked_worktree(repo)
    marker = worktree / ".git"
    gitdir = repo / ".git" / "worktrees" / "feature"
    if broken_side == "marker":
        marker.write_text(
            f"gitdir: {tmp_path / 'unowned-gitdir'}\n",
            encoding="utf-8",
        )
    else:
        (gitdir / "gitdir").write_text(
            f"{tmp_path / 'different-marker'}\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [_policy(repo)],
    )

    profile = shell_guard.pinned_repo_sandbox_profile(
        "pytest",
        str(tmp_path),
        grant_policies=[_policy(repo)],
    )

    assert _git_allow(repo) not in profile
    assert (
        f'(allow file-read* file-write* (subpath "{worktree}"))'
        not in profile
    )


@pytest.mark.parametrize(
    "policy_kwargs",
    [
        {"source_binding_valid": False},
        {"enforcement_error": "ambiguous repository policy"},
    ],
)
def test_unenforceable_policy_gets_no_ordinary_git_grant(
    policy_kwargs,
    tmp_path,
    monkeypatch,
) -> None:
    repo = _git_repo(tmp_path / "repo")
    _linked_worktree(repo)
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [_policy(repo, **policy_kwargs)],
    )

    profile = shell_guard.pinned_repo_sandbox_profile(
        "pytest",
        str(tmp_path),
        grant_policies=[_policy(repo, **policy_kwargs)],
    )

    assert _git_allow(repo) not in profile


def _run_protected_shell(
    root: Path,
    cwd: Path,
    command: str,
    name: str,
    *,
    grant_policies: list[RepoPolicy],
) -> subprocess.CompletedProcess[str]:
    profile_path = root / f"{name}.sb"
    profile_path.write_text(
        shell_guard.execution_envelope_profile(
            command,
            cwd,
            profile_path,
            leased=False,
            grant_policies=grant_policies,
        ),
        encoding="utf-8",
    )
    return subprocess.run(
        shell_guard.safehouse_argv(command, cwd, profile_path),
        capture_output=True,
        text=True,
        env=shell_guard.safehouse_subprocess_env("thread-1"),
        timeout=20,
    )


@pytest.mark.integration
def test_verified_worktree_git_effects_are_cwd_independent_and_controls_stay_denied(
    tmp_path,
    monkeypatch,
) -> None:
    if shell_guard.shutil.which("safehouse") is None:
        pytest.skip("Agent Safehouse is unavailable")
    repo = _git_repo(tmp_path / "repo")
    worktree = _linked_worktree(repo)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(
        shell_guard,
        "list_registered_repo_policies",
        lambda: [_policy(repo)],
    )
    # The cwd grant must not cover the sibling repo: its write access must
    # come from the verified-worktree profile, not Safehouse --add-dirs.
    monkeypatch.setattr(shell_guard, "get_allowed_dirs_rw", lambda: [outside])
    monkeypatch.setattr(shell_guard, "get_sandbox_add_dirs_rw", lambda _thread_id: [outside])
    monkeypatch.setattr(shell_guard, "get_allowed_dirs_ro", lambda: [])

    staged = worktree / "staged.txt"
    success = _run_protected_shell(
        tmp_path,
        outside,
        f"printf staged > {shlex.quote(str(staged))} && "
        f"/usr/bin/git -C {shlex.quote(str(worktree))} add staged.txt",
        "ordinary-git",
        grant_policies=[_policy(repo)],
    )
    assert success.returncode == 0, success.stderr
    staged_names = subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(worktree),
            "diff",
            "--cached",
            "--name-only",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert staged_names.stdout.splitlines() == ["staged.txt"]

    protected_files = {
        "base": repo / "tracked.txt",
        "head": repo / ".git" / "HEAD",
        "config": repo / ".git" / "config",
        "marker": worktree / ".git",
    }
    originals = {
        name: path.read_text(encoding="utf-8")
        for name, path in protected_files.items()
    }
    for name, path in protected_files.items():
        denied = _run_protected_shell(
            tmp_path,
            outside,
            f"printf forbidden > {shlex.quote(str(path))}",
            f"deny-{name}",
            grant_policies=[_policy(repo)],
        )
        assert denied.returncode != 0, name
        assert path.read_text(encoding="utf-8") == originals[name]
