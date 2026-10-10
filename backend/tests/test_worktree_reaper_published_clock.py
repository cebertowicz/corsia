"""The 24 h idle clock for clean worktrees whose commits are already on a remote."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time

import pytest

from infra import repo_flock, worktree_reaper, worktree_reaper_checks, worktree_reaper_store
from infra.project_repo_policy import RepoPolicy
from infra.worktree_reaper import reap
from infra.worktree_reaper_probe import ProcessProbe, admin_dir

HOUR = 3600


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
    monkeypatch.setattr(worktree_reaper_checks, "open_pr_branches", lambda repo: set())


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "README").write_text("hi\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _worktree(repo: Path, name: str, *, pushed: bool, age_hours: float) -> Path:
    """Add a clean worktree on branch ``name``; ``pushed`` mirrors it on a remote ref."""
    _git(repo, "worktree", "add", "-q", "-b", name, f".worktrees/{name}")
    wt = repo / ".worktrees" / name
    if pushed:
        _git(repo, "update-ref", f"refs/remotes/origin/{name}", f"refs/heads/{name}")
    admin = admin_dir(wt)
    stamp = time.time() - age_hours * HOUR
    for path in (admin / "HEAD", admin / "index", admin / "logs" / "HEAD",
                 admin / "ORIG_HEAD", repo / ".git" / "logs" / "refs" / "heads" / name, wt):
        if path.exists():
            os.utime(path, (stamp, stamp))
    return wt


def _run(repo: Path):
    policy = RepoPolicy(
        project_id="p", repo_name="repo", path=repo,
        pinned_branch=None, protect_base_checkout=False,
    )
    return reap(policy, mode="idle", probe=FakeProbe())


def test_threshold_constants_are_literal():
    assert worktree_reaper.PUBLISHED_IDLE_THRESHOLD_SECONDS == 24 * HOUR
    assert worktree_reaper.IDLE_THRESHOLD_SECONDS == 7 * 24 * HOUR


def test_clean_pushed_no_pr_worktree_is_reaped_at_25_hours(repo):
    wt = _worktree(repo, "pushed", pushed=True, age_hours=25)

    report = _run(repo)

    assert report.removed == 1 and report.skips == []
    assert not wt.exists()


def test_clean_pushed_no_pr_worktree_is_kept_at_23_hours(repo):
    wt = _worktree(repo, "pushed", pushed=True, age_hours=23)

    report = _run(repo)

    assert report.removed == 0
    assert {item.name: item.reason for item in report.skips} == {"pushed": "recent"}
    assert wt.is_dir()


def test_unique_commits_are_kept_at_25_hours(repo):
    wt = _worktree(repo, "local", pushed=False, age_hours=25)

    report = _run(repo)

    assert {item.name: item.reason for item in report.skips} == {"local": "recent"}
    assert wt.is_dir()


def test_dirty_worktree_is_kept_at_25_hours(repo):
    wt = _worktree(repo, "dirty", pushed=True, age_hours=25)
    (wt / "README").write_text("changed\n")
    stamp = time.time() - 25 * HOUR
    os.utime(wt, (stamp, stamp))

    report = _run(repo)

    assert {item.name: item.reason for item in report.skips} == {"dirty": "dirty"}
    assert wt.is_dir()


def test_open_pr_branch_is_kept_at_25_hours(repo, monkeypatch):
    monkeypatch.setattr(worktree_reaper_checks, "open_pr_branches", lambda repo: {"has-pr"})
    wt = _worktree(repo, "has-pr", pushed=True, age_hours=25)

    report = _run(repo)

    assert {item.name: item.reason for item in report.skips} == {"has-pr": "recent"}
    assert wt.is_dir()


def test_unknown_pr_state_keeps_the_seven_day_clock(repo, monkeypatch):
    monkeypatch.setattr(worktree_reaper_checks, "open_pr_branches", lambda repo: None)
    wt = _worktree(repo, "unknown", pushed=True, age_hours=25)

    report = _run(repo)

    assert {item.name: item.reason for item in report.skips} == {"unknown": "recent"}
    assert wt.is_dir()


def test_unpublished_check_fails_closed_on_git_error(repo):
    from infra.worktree_reaper_probe import has_unpublished_commits

    assert has_unpublished_commits(repo, "0" * 40) is True


def test_rev_list_timeout_keeps_the_worktree_and_the_pass_continues(repo, monkeypatch):
    from infra import worktree_reaper_probe

    first = _worktree(repo, "first", pushed=True, age_hours=25)
    second = _worktree(repo, "second", pushed=True, age_hours=25)
    real_git = worktree_reaper_probe.git
    timeouts: list[list[str]] = []

    def git_with_one_timeout(cwd, args, **kwargs):
        if args[:1] == ["rev-list"] and not timeouts:
            timeouts.append(args)
            raise worktree_reaper_probe.ReaperGitError("git rev-list failed: timed out")
        return real_git(cwd, args, **kwargs)

    monkeypatch.setattr(worktree_reaper_probe, "git", git_with_one_timeout)

    report = _run(repo)

    assert len(timeouts) == 1
    assert report.removed == 1
    assert [item.reason for item in report.skips] == ["recent"]
    assert sorted(path.exists() for path in (first, second)) == [False, True]
