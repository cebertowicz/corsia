"""Regression coverage for stale PR-terminal worktree records."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time

import pytest

from infra import repo_flock, worktree_reaper_checks, worktree_reaper_store
from infra.project_repo_policy import RepoPolicy
from infra.worktree_reaper import reap
from infra.worktree_reaper_probe import ProcessProbe, admin_dir


DAY = 86400


class FakeProbe(ProcessProbe):
    """Avoid the unavailable process inspection dependency in this test."""

    def refresh(self) -> None:
        return None

    def skip_reason(self, worktree: Path) -> str | None:
        return None


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    monkeypatch.setattr(repo_flock, "_lock_dir", lambda: state / "locks")
    monkeypatch.setattr(
        worktree_reaper_store, "_store_path", lambda: state / "reaper" / "pr.json",
    )
    monkeypatch.setattr(worktree_reaper_checks, "_load_active_threads", lambda: set())


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "README").write_text("hi\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _policy(repo: Path) -> RepoPolicy:
    return RepoPolicy(
        project_id="p", repo_name="repo", path=repo,
        pinned_branch=None, protect_base_checkout=False,
    )


def _age(repo: Path, worktree: Path, seconds: float) -> None:
    stamp = time.time() - seconds
    admin = admin_dir(worktree)
    assert admin is not None
    branch = (admin / "HEAD").read_text().strip().removeprefix("ref: refs/heads/")
    for path in (
        admin / "HEAD", admin / "index", admin / "logs" / "HEAD", admin / "ORIG_HEAD",
        repo / ".git" / "logs" / "refs" / "heads" / branch, worktree,
    ):
        if path.exists():
            os.utime(path, (stamp, stamp))


def test_idle_reaper_forgets_a_record_whose_head_advanced(repo: Path) -> None:
    """An advanced worktree returns to the ordinary seven-day idle policy."""
    worktree = repo / ".worktrees" / "mine"
    _git(repo, "worktree", "add", "-q", "-b", "feature", str(worktree))
    recorded_head = _git(worktree, "rev-parse", "HEAD")
    (worktree / "later.txt").write_text("new work\n")
    _git(worktree, "add", "later.txt")
    _git(worktree, "commit", "-q", "-m", "after merge")
    worktree_reaper_store.remember_pr_terminal(worktree, recorded_head)

    _age(repo, worktree, 2 * 3600)
    first = reap(_policy(repo), mode="idle", probe=FakeProbe())

    assert {item.name: item.reason for item in first.skips} == {"mine": "recent"}
    assert worktree_reaper_store.pr_terminal_heads() == {}
    assert worktree.is_dir()

    _age(repo, worktree, 8 * DAY)
    second = reap(_policy(repo), mode="idle", probe=FakeProbe())

    assert second.removed == 1
    assert not worktree.exists()
