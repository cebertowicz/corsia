"""Git worktree grouping provider — live/dead/spoof resolution.

Uses real ``git`` repos/worktrees so the reciprocal-gitdir verification in
``tools.sandbox_config.repo_worktree_root`` is exercised for real, not mocked.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from infra.project_repo_policy import RepoPolicy
from registry import grouping
from tools.sandbox_config import repo_worktree_root


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _init_repo(base: Path) -> Path:
    repo = base / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    return repo


def _add_worktree(repo: Path, branch: str, name: str) -> Path:
    worktrees_dir = repo / ".worktrees"
    worktrees_dir.mkdir(exist_ok=True)
    wt_path = worktrees_dir / name
    _git("worktree", "add", "-q", "-b", branch, str(wt_path), cwd=repo)
    return wt_path


def _policy(repo: Path) -> RepoPolicy:
    return RepoPolicy(
        project_id="p1", repo_name="app", path=repo,
        pinned_branch=None, protect_base_checkout=False,
    )


@pytest.fixture
def repo(tmp_path):
    return _init_repo(tmp_path)


def test_live_worktree_groups_file_by_verified_root(repo):
    wt = _add_worktree(repo, "feat/x", "9e16-feat-x")
    target = wt / "file.py"
    target.write_text("x")

    assignment = grouping.group_for_path(str(target), policies=[_policy(repo)], cache={})

    assert assignment is not None
    assert assignment.provider == "git-worktree"
    assert assignment.key == str(wt.resolve())
    expected = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=str(wt), check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert assignment.branch == expected == "feat/x"
    assert expected in assignment.label


def test_detached_worktree_falls_back_to_directory_name(repo):
    wt = _add_worktree(repo, "feat/detached", "9e16-detached")
    target = wt / "file.py"
    target.write_text("x")
    _git("checkout", "--detach", cwd=wt)

    assignment = grouping.group_for_path(str(target), policies=[_policy(repo)], cache={})

    assert assignment is not None
    assert assignment.branch == wt.name
    assert assignment.label == f"{wt.name} · {wt.name}"


def test_reftable_worktree_falls_back_to_directory_name(repo):
    wt = _add_worktree(repo, "feat/reftable", "9e16-reftable")
    target = wt / "file.py"
    target.write_text("x")
    gitdir = Path((wt / ".git").read_text().strip().split(":", 1)[1].strip())
    (repo / ".git" / "reftable").mkdir()
    (gitdir / "HEAD").write_text("ref: refs/heads/.invalid\n")

    assignment = grouping.group_for_path(str(target), policies=[_policy(repo)], cache={})

    assert assignment is not None
    assert assignment.branch == wt.name
    assert assignment.label == f"{wt.name} · {wt.name}"


def test_two_files_in_same_worktree_share_one_group_key(repo):
    wt = _add_worktree(repo, "feat/shared", "9e16-shared")
    (wt / "a.py").write_text("a")
    (wt / "sub").mkdir()
    (wt / "sub" / "b.py").write_text("b")
    cache: dict = {}
    policies = [_policy(repo)]

    a = grouping.group_for_path(str(wt / "a.py"), policies=policies, cache=cache)
    b = grouping.group_for_path(str(wt / "sub" / "b.py"), policies=policies, cache=cache)

    assert a.key == b.key == str(wt.resolve())


def test_same_worktree_reuses_its_verified_root(repo, monkeypatch):
    """A pane can contain thousands of distinct paths in one worktree."""
    wt = _add_worktree(repo, "feat/cached", "9e16-cached")
    (wt / "a.py").write_text("a")
    (wt / "sub").mkdir()
    (wt / "sub" / "b.py").write_text("b")
    actual = Path.read_text
    marker_reads = 0

    def counted(path, *args, **kwargs):
        nonlocal marker_reads
        if path.name in {".git", "gitdir"}:
            marker_reads += 1
        return actual(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counted)
    cache: dict = {}
    policies = [_policy(repo)]

    assert grouping.group_for_path(str(wt / "a.py"), policies=policies, cache=cache)
    assert grouping.group_for_path(str(wt / "sub" / "b.py"), policies=policies, cache=cache)

    assert marker_reads == 2


def test_file_directly_in_base_checkout_is_not_grouped(repo):
    (repo / "top.py").write_text("x")

    assignment = grouping.group_for_path(
        str(repo / "top.py"), policies=[_policy(repo)], cache={},
    )

    assert assignment is None


def test_file_outside_any_registered_repo_is_not_grouped(tmp_path, repo):
    outside = tmp_path / "elsewhere.py"
    outside.write_text("x")

    assignment = grouping.group_for_path(
        str(outside), policies=[_policy(repo)], cache={},
    )

    assert assignment is None


def test_worktree_reached_through_repo_alias_is_verified_and_grouped(repo):
    worktree = _add_worktree(repo, "feat/alias", "9e16-alias")
    target = worktree / "file.py"
    target.write_text("alias")
    alias = repo.parent / "repo-alias"
    alias.symlink_to(repo, target_is_directory=True)
    alias_path = alias / ".worktrees" / worktree.name / target.name

    assert repo_worktree_root(alias_path, repo) == worktree.resolve()
    assignment = grouping.group_for_path(
        str(alias_path), policies=[_policy(repo)], cache={},
    )
    assert assignment is not None
    assert assignment.key == str(worktree.resolve())


def test_symlinked_file_into_another_worktree_groups_with_its_target(repo):
    first = _add_worktree(repo, "feat/first", "9e16-first")
    second = _add_worktree(repo, "feat/second", "9e16-second")
    target = second / "target.py"
    target.write_text("target")
    alias = first / "alias.py"
    alias.symlink_to(target)

    assignment = grouping.group_for_path(
        str(alias), policies=[_policy(repo)], cache={},
    )

    assert assignment is not None
    assert assignment.key == str(second.resolve())
    assert assignment.key != str(first.resolve())


def test_symlinked_file_outside_a_worktree_is_not_grouped(repo):
    worktree = _add_worktree(repo, "feat/escape", "9e16-escape")
    outside = repo.parent / "outside.py"
    outside.write_text("outside")
    alias = worktree / "escape.py"
    alias.symlink_to(outside)

    assert grouping.group_for_path(
        str(alias), policies=[_policy(repo)], cache={},
    ) is None


def test_worktree_removed_via_git_is_no_longer_grouped(repo):
    wt = _add_worktree(repo, "feat/gone", "9e16-gone")
    target = wt / "file.py"
    target.write_text("x")
    assert grouping.group_for_path(
        str(target), policies=[_policy(repo)], cache={},
    ) is not None

    _git("worktree", "remove", "--force", str(wt), cwd=repo)

    assignment = grouping.group_for_path(str(target), policies=[_policy(repo)], cache={})
    assert assignment is None


def test_worktree_with_corrupted_internal_gitdir_is_dead(repo):
    """The directory/.git pointer file survive, but git's own worktrees/<name>
    metadata is gone — the reciprocal check must catch this, not just presence
    of a .git file."""
    wt = _add_worktree(repo, "feat/corrupt", "9e16-corrupt")
    target = wt / "file.py"
    target.write_text("x")
    assert grouping.group_for_path(
        str(target), policies=[_policy(repo)], cache={},
    ) is not None

    shutil.rmtree(repo / ".git" / "worktrees" / "9e16-corrupt")

    assignment = grouping.group_for_path(str(target), policies=[_policy(repo)], cache={})
    assert assignment is None


def test_symlinked_git_file_spoof_is_rejected(repo):
    """A worktree-shaped directory whose .git is a symlink (not a real
    gitdir-pointer file) must never be treated as a verified worktree."""
    worktrees_dir = repo / ".worktrees"
    worktrees_dir.mkdir(exist_ok=True)
    fake = worktrees_dir / "fake-worktree"
    fake.mkdir()
    elsewhere = repo.parent / "elsewhere-gitdir"
    elsewhere.mkdir()
    (fake / ".git").symlink_to(elsewhere)
    target = fake / "file.py"
    target.write_text("x")

    assignment = grouping.group_for_path(str(target), policies=[_policy(repo)], cache={})

    assert assignment is None


def test_gitdir_pointing_outside_repo_worktrees_is_rejected(repo):
    """A .git pointer file that resolves outside repo/.git/worktrees/ must not
    verify, even though it is a real file with real gitdir: syntax."""
    worktrees_dir = repo / ".worktrees"
    worktrees_dir.mkdir(exist_ok=True)
    fake = worktrees_dir / "escape-attempt"
    fake.mkdir()
    bogus_gitdir = repo.parent / "bogus-gitdir"
    bogus_gitdir.mkdir()
    (fake / ".git").write_text(f"gitdir: {bogus_gitdir}\n")
    target = fake / "file.py"
    target.write_text("x")

    assignment = grouping.group_for_path(str(target), policies=[_policy(repo)], cache={})

    assert assignment is None


def test_url_path_is_never_grouped():
    assert grouping.group_for_path(
        "https://github.com/x/y/pull/1", policies=[], cache={},
    ) is None


def test_none_path_is_never_grouped():
    assert grouping.group_for_path(None, policies=[], cache={}) is None
