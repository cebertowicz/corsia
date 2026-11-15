"""Constant-size repository rules, attacked through real Safehouse in L3.

No Python path guard stands between these commands and Seatbelt. Local nested
sandboxes cannot apply, so a local skip proves nothing about confinement.
"""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile

import pytest

from infra.project_repo_policy import RepoPolicy
from tools import shell_guard
from tools.base_git_paths import base_git_control_paths, base_git_identity_paths


def test_worktree_profile_is_identical_at_zero_and_400_worktrees(tmp_path, monkeypatch):
    repo = tmp_path.resolve() / "repo"
    policy = RepoPolicy("project", "repo", repo, "main", True)
    monkeypatch.setattr(shell_guard, "list_unsupported_pinned_repos", lambda _: [])
    profiles = []
    for count in (0, 400):
        trees = [repo / ".worktrees" / f"tree-{i:04d}" for i in range(count)]
        monkeypatch.setattr(shell_guard, "_verified_worktrees", lambda *_: trees)
        profiles.append(shell_guard.pinned_repo_sandbox_profile(
            "printf probe", str(repo), policies=[policy],
        ))
    assert profiles[0] == profiles[1]
    assert str(repo / ".worktrees") in profiles[0]


class RepositorySandbox:
    def __init__(self, root, policy, safehouse):
        self.root, self.policy, self.safehouse = root, policy, safehouse
        self.repo = policy.path
        self.trees = self.repo / ".worktrees"
        self.own = self.trees / "own"
        self.peer = self.trees / "peer"
        self.env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Sandbox Test",
            "GIT_AUTHOR_EMAIL": "sandbox@example.invalid",
            "GIT_COMMITTER_NAME": "Sandbox Test",
            "GIT_COMMITTER_EMAIL": "sandbox@example.invalid",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
        }

    def setup_git(self, *args, cwd=None):
        return subprocess.run(
            ["git", *args], cwd=cwd or self.repo, env=self.env,
            capture_output=True, text=True, timeout=20, check=True,
        )

    def run(self, command, *, cwd=None):
        cwd = cwd or self.own
        profile = self.root / "repo.sb"
        # Exercise the strongest ordinary metadata grant as well as direct Git.
        profile.write_text(shell_guard.pinned_repo_sandbox_profile(
            command, str(cwd), policies=[self.policy], grant_policies=[self.policy],
        ))
        return subprocess.run(
            [self.safehouse, "--workdir=", f"--add-dirs={self.root}",
             "--trust-workdir-config=false", f"--append-profile={profile}",
             "--", "/bin/bash", "-c", command],
            cwd=cwd, env=self.env, capture_output=True, text=True, timeout=20,
        )

    def allowed(self, command, *, cwd=None):
        result = self.run(command, cwd=cwd)
        assert result.returncode == 0, (command, result.stdout, result.stderr)
        return result

    def denied(self, command):
        result = self.run(command)
        assert result.returncode != 0, f"sandbox allowed: {command}"
        assert "Operation not permitted" in result.stderr or "Permission denied" in result.stderr, (
            command, result.returncode, result.stderr,
        )


@pytest.fixture
def repository_sandbox(monkeypatch):
    required = os.environ.get("CORSIA_REQUIRE_SAFEHOUSE") == "1"
    reason = None
    if os.environ.get("CORSIA_THREAD_ID"):
        reason = "agent shells cannot prove nested Safehouse confinement"
    safehouse = shutil.which("safehouse")
    if safehouse is None:
        reason = "Agent Safehouse is unavailable"
    if reason:
        if required:
            pytest.fail(f"CORSIA_REQUIRE_SAFEHOUSE=1: {reason}")
        pytest.skip(reason)
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        root = Path(directory).resolve()
        repo = root / "repo"
        repo.mkdir()
        policy = RepoPolicy("project", "repo", repo, "main", True)
        sandbox = RepositorySandbox(root, policy, safehouse)
        sandbox.setup_git("init", "-b", "main")
        (repo / "base.txt").write_text("base\n")
        sandbox.setup_git("add", "base.txt")
        sandbox.setup_git("commit", "-m", "Initial fixture")
        sandbox.setup_git("worktree", "add", "-b", "own", str(sandbox.own))
        sandbox.setup_git("worktree", "add", "-b", "peer", str(sandbox.peer))
        monkeypatch.setattr(shell_guard, "list_registered_repo_policies", lambda: [policy])
        monkeypatch.setattr(shell_guard, "list_unsupported_pinned_repos", lambda _: [])
        # Fail on a broken sandbox launch instead of mistaking it for denial.
        sandbox.allowed("printf launched > launch-check && cat launch-check")
        yield sandbox


@pytest.mark.integration
@pytest.mark.parametrize("relative", [
    "base.txt", "new-base.txt", ".git/HEAD", ".git/index", ".git/logs/HEAD",
    ".git/refs/heads/main", ".git/refs/heads/main.lock",
    ".git/logs/refs/heads/main", ".git/logs/refs/heads/main.lock",
    ".git/commondir", ".git/config", ".git/config.lock", ".git/packed-refs",
    ".git/packed-refs.lock", ".git/COMMONDIR", ".git/HoOkS/pre-commit",
])
def test_real_worktree_folder_keeps_base_and_git_controls_readonly(repository_sandbox, relative):
    sandbox = repository_sandbox
    target = sandbox.repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    before = target.read_bytes() if target.exists() else None
    sandbox.denied(f"printf changed > {shlex.quote(str(target))}")
    assert (target.read_bytes() if target.exists() else None) == before


@pytest.mark.integration
@pytest.mark.parametrize("attack", ["rename", "delete-empty", "replace-empty"])
def test_real_worktree_folder_container_cannot_move_or_disappear(repository_sandbox, attack):
    sandbox = repository_sandbox
    trees = shlex.quote(str(sandbox.trees))
    if attack == "rename":
        command = f"mv {trees} {shlex.quote(str(sandbox.root / 'moved'))}"
    else:
        # Empty is the strongest rmdir/rename-over case, not ENOTEMPTY.
        sandbox.setup_git("worktree", "remove", "--force", str(sandbox.own))
        sandbox.setup_git("worktree", "remove", "--force", str(sandbox.peer))
        replacement = sandbox.root / "replacement"
        replacement.mkdir()
        command = (f"rmdir {trees}" if attack == "delete-empty" else
                   f"/usr/bin/perl -e 'rename($ARGV[0], $ARGV[1]) or die $!' {shlex.quote(str(replacement))} {trees}")
    # cwd must survive the empty-container setup.
    result = sandbox.run(command, cwd=sandbox.repo)
    assert result.returncode != 0, command
    assert "Operation not permitted" in result.stderr or "Permission denied" in result.stderr, result.stderr
    assert sandbox.trees.is_dir()


@pytest.mark.integration
@pytest.mark.parametrize("spelling", [".git", ".GIT", ".Git"])
@pytest.mark.parametrize("attack", ["write", "unlink", "replace", "rename-away"])
def test_real_worktree_folder_existing_pointer_cannot_mutate(repository_sandbox, spelling, attack):
    sandbox = repository_sandbox
    marker = sandbox.own / ".git"
    before = marker.read_bytes()
    # On a case-sensitive runner also test an existing alternate spelling.
    alternate = sandbox.own / spelling
    if not alternate.exists():
        alternate.write_bytes(before)
    target = shlex.quote(str(alternate))
    replacement = sandbox.own / "replacement"
    replacement.write_text("gitdir: /tmp/forged\n")
    commands = {
        "write": f"printf changed > {target}",
        "unlink": f"rm {target}",
        "replace": f"mv {shlex.quote(str(replacement))} {target}",
        "rename-away": f"mv {target} {shlex.quote(str(sandbox.own / 'stolen'))}",
    }
    sandbox.denied(commands[attack])
    assert marker.read_bytes() == before
    assert alternate.read_bytes() == before


@pytest.mark.integration
@pytest.mark.parametrize("spelling", [".git", ".GIT", ".Git"])
@pytest.mark.parametrize("kind", ["file", "directory"])
def test_real_worktree_folder_new_pointer_cannot_be_created(repository_sandbox, spelling, kind):
    sandbox = repository_sandbox
    fresh = sandbox.trees / "fresh"
    fresh.mkdir()  # Unregistered, newly created immediate folder.
    target = fresh / spelling
    command = (f"printf forged > {shlex.quote(str(target))}" if kind == "file"
               else f"mkdir {shlex.quote(str(target))}")
    sandbox.denied(command)
    assert not target.exists()


@pytest.mark.integration
@pytest.mark.parametrize("spelling", [".git", ".GIT", ".Git"])
def test_real_worktree_folder_prepared_pointer_cannot_be_renamed_up(repository_sandbox, spelling):
    sandbox = repository_sandbox
    parent = sandbox.trees / "x"
    parent.mkdir()
    prepared = parent / "y"
    target = sandbox.trees / "z"
    # Preparation itself remains allowed, including nested Git repositories.
    sandbox.allowed(f"mkdir {shlex.quote(str(prepared))} && printf forged > {shlex.quote(str(prepared / spelling))}")
    sandbox.denied(f"mv {shlex.quote(str(prepared))} {shlex.quote(str(target))}")
    assert (prepared / spelling).read_text() == "forged"
    assert not target.exists()


@pytest.mark.integration
@pytest.mark.parametrize("destination", ["base", "base-git", "peer"])
@pytest.mark.parametrize("location", ["immediate", "nested"])
@pytest.mark.parametrize("attack", ["write", "unlink", "replace"])
def test_real_worktree_folder_symlink_cannot_bypass_denies(repository_sandbox, destination, location, attack):
    sandbox = repository_sandbox
    link = (sandbox.trees if location == "immediate" else sandbox.own) / "alias"
    actual = sandbox.peer if destination == "peer" else sandbox.repo
    if location == "immediate":
        link.symlink_to(actual)  # An existing alias, before the sandbox starts.
    else:
        sandbox.allowed(f"ln -s {shlex.quote(str(actual))} {shlex.quote(str(link))}")
    relative = {"base": "base.txt", "base-git": ".git/HEAD", "peer": ".git"}[destination]
    target = link / relative
    before = (actual / relative).read_bytes()
    replacement = sandbox.own / "replacement"
    replacement.write_text("forged")
    commands = {
        "write": f"printf forged > {shlex.quote(str(target))}",
        "unlink": f"rm {shlex.quote(str(target))}",
        "replace": f"mv {shlex.quote(str(replacement))} {shlex.quote(str(target))}",
    }
    sandbox.denied(commands[attack])
    assert (actual / relative).read_bytes() == before
    if destination == "peer":
        sandbox.allowed(f"printf allowed > {shlex.quote(str(link / 'ordinary'))}")
        assert (sandbox.peer / "ordinary").read_text() == "allowed"


@pytest.mark.integration
def test_real_worktree_folder_preserves_worktree_git_and_nested_repositories(repository_sandbox):
    sandbox = repository_sandbox
    snapshot = {path: path.read_bytes() if path.exists() else None for path in (
        *base_git_control_paths(sandbox.repo), *base_git_identity_paths(sandbox.repo),
    )}
    for tree in (sandbox.own, sandbox.peer):
        sandbox.allowed("cat base.txt && printf content > ordinary", cwd=tree)
        sandbox.allowed("git status --porcelain", cwd=tree)
        sandbox.allowed("git add ordinary", cwd=tree)
        sandbox.allowed(f"git commit -m {shlex.quote(tree.name)}", cwd=tree)
        assert (tree / "ordinary").read_text() == "content"
    # Rebase divergent branches without a content conflict.
    (sandbox.peer / "upstream").write_text("upstream")
    sandbox.setup_git("add", "upstream", cwd=sandbox.peer)
    sandbox.setup_git("commit", "-m", "Upstream fixture", cwd=sandbox.peer)
    sandbox.allowed("git rebase peer")
    remote = sandbox.root / "remote.git"
    sandbox.setup_git("clone", "--bare", str(sandbox.repo), str(remote))
    sandbox.setup_git("remote", "add", "origin", str(remote))
    # Adding the fixture remote intentionally changes config outside confinement.
    snapshot[sandbox.repo / ".git" / "config"] = (sandbox.repo / ".git" / "config").read_bytes()
    sandbox.allowed("git fetch origin")
    nested = sandbox.own / "nested"
    sandbox.allowed(f"git init {shlex.quote(str(nested))}")
    sandbox.allowed("printf nested > file", cwd=nested)
    sandbox.allowed("git add file", cwd=nested)
    sandbox.allowed("git commit -m nested", cwd=nested)
    for path, before in snapshot.items():
        assert (path.read_bytes() if path.exists() else None) == before


@pytest.mark.integration
def test_real_worktree_folder_preserves_authorized_base_worktree_add_remove(repository_sandbox):
    sandbox = repository_sandbox
    sandbox.setup_git("branch", "existing")
    target = sandbox.trees / "added"
    sandbox.allowed(f"git worktree add {shlex.quote(str(target))} existing", cwd=sandbox.repo)
    assert (target / ".git").is_file()
    sandbox.allowed(f"git worktree remove {shlex.quote(str(target))}", cwd=sandbox.repo)
    assert not target.exists()


@pytest.mark.integration
def test_real_worktree_folder_symlink_container_cannot_grant_the_base(repository_sandbox):
    sandbox = repository_sandbox
    sandbox.trees.rename(sandbox.root / "original-trees")
    sandbox.trees.symlink_to(sandbox.repo)
    target = sandbox.repo / "base.txt"
    before = target.read_bytes()
    result = sandbox.run(f"printf forged > {shlex.quote(str(target))}", cwd=sandbox.repo)
    assert result.returncode != 0, "redirected .worktrees reopened the protected base"
    assert "Operation not permitted" in result.stderr or "Permission denied" in result.stderr, result.stderr
    assert target.read_bytes() == before
