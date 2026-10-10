"""Race classification in the worktree reaper's quarantine removal."""

from __future__ import annotations

from pathlib import Path
import subprocess

from infra import worktree_reaper_remove
from infra.worktree_reaper_probe import git_detail


def _done(code: int, err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, "", err)


def test_dirty_refusal_on_a_long_path_is_a_race_not_pending(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    deep = "/private/var/folders/" + "x" * 260 + "/repo/.git/corsia-reaping/racy-1"
    refusal = f"fatal: '{deep}' contains modified or untracked files, use --force to delete it"
    assert "contains modified or untracked files" not in git_detail(_done(128, refusal))
    calls: list[list[str]] = []

    def _git(_repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:2] == ["worktree", "remove"]:
            return _done(128, refusal)
        return _done(0)

    monkeypatch.setattr(worktree_reaper_remove, "git", _git)
    worktree = repo / ".worktrees" / "racy"

    assert worktree_reaper_remove.remove_worktree(repo, worktree, "racy") == "raced_dirty"
    assert calls[-1][:2] == ["worktree", "move"] and calls[-1][-1] == str(worktree)
