from __future__ import annotations

import subprocess
from pathlib import Path

from service_script_fixtures import run_bash


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, text=True, capture_output=True,
    ).stdout.strip()


def prepare(repo: Path) -> subprocess.CompletedProcess:
    return run_bash(
        '''
fail() { printf '%s\\n' "$*" >&2; }
source scripts/corsia.d/runtime.sh
recover_stale_git_index_lock() { return 0; }
redeploy_sync_python_dependencies() { return 0; }
redeploy_build() { return 0; }
prepare_redeploy_locked "$CHECKOUT" "$CHECKOUT/.git"
''',
        env={"CHECKOUT": str(repo)},
    )


def test_no_upstream_refusal_and_retry(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "code.txt").write_text("original code\n")
    git(repo, "add", "code.txt")
    git(repo, "commit", "-m", "initial")
    before = git(repo, "rev-parse", "HEAD")
    result = prepare(repo)
    assert result.returncode == 47
    assert "Redeploy refused: branch main has no upstream — checkout left untouched" in result.stderr
    assert git(repo, "rev-parse", "HEAD") == before
    assert git(repo, "status", "--porcelain") == ""
    assert (repo / "code.txt").read_text() == "original code\n"
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "--set-upstream", "origin", "main")
    assert prepare(repo).returncode == 0


def test_diverged_refusal_and_retry(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "code.txt").write_text("original code\n")
    git(repo, "add", "code.txt")
    git(repo, "commit", "-m", "initial")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "--set-upstream", "origin", "main")
    peer = tmp_path / "peer"
    subprocess.run(["git", "clone", "--branch", "main", str(remote), str(peer)], check=True, capture_output=True)
    git(peer, "config", "user.email", "test@example.invalid")
    git(peer, "config", "user.name", "Test")
    git(peer, "commit", "--allow-empty", "-m", "remote")
    git(peer, "push")
    git(repo, "commit", "--allow-empty", "-m", "local")
    before = git(repo, "rev-parse", "HEAD")
    tracked_before = git(repo, "rev-parse", "origin/main")
    result = prepare(repo)
    assert result.returncode == 48
    assert "Redeploy refused: branch main and its upstream have diverged — checkout left untouched" in result.stderr
    assert "Not possible to fast-forward" in result.stderr
    assert git(repo, "rev-parse", "HEAD") == before
    assert git(repo, "status", "--porcelain") == ""
    assert (repo / "code.txt").read_text() == "original code\n"
    assert git(repo, "rev-parse", "origin/main") != tracked_before
    assert git(repo, "rev-parse", "origin/main") == git(peer, "rev-parse", "HEAD")
    git(repo, "rebase", "origin/main")
    assert prepare(repo).returncode == 0
