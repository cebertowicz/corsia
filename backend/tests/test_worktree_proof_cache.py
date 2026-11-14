"""Worktree grant changes must be visible immediately between listings."""

import os
import shutil

import pytest

from tools.sandbox_config import list_repo_worktrees


def linked(repo, name):
    worktree = repo / ".worktrees" / name
    gitdir = repo / ".git" / "worktrees" / name
    worktree.mkdir(parents=True)
    gitdir.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n")
    (gitdir / "gitdir").write_text(f"{worktree / '.git'}\n")
    return worktree, gitdir


@pytest.mark.parametrize("change", ["add", "remove", "marker", "reciprocal", "entry_symlink", "marker_symlink"])
def test_worktree_changes_are_seen(tmp_path, change):
    repo = (tmp_path / "repo").resolve()
    worktree, gitdir = linked(repo, "one")
    assert list_repo_worktrees(repo) == [worktree]
    assert list_repo_worktrees(repo) == [worktree]
    if change == "add":
        other, _ = linked(repo, "two")
        expected = [worktree, other]
    elif change == "remove":
        shutil.rmtree(worktree)
        expected = []
    elif change == "marker":
        other, other_gitdir = linked(tmp_path / "other", "other")
        (worktree / ".git").write_text(f"gitdir: {other_gitdir}\n")
        expected = []
    elif change == "reciprocal":
        (gitdir / "gitdir").write_text(f"{repo / 'other' / '.git'}\n")
        expected = []
    elif change == "entry_symlink":
        moved = tmp_path / "moved"
        worktree.rename(moved)
        worktree.symlink_to(moved, target_is_directory=True)
        expected = []
    else:
        marker = worktree / ".git"
        marker.unlink()
        marker.symlink_to(gitdir / "gitdir")
        expected = []
    assert list_repo_worktrees(repo) == expected


def test_unchanged_canonical_proof_is_reused(tmp_path, monkeypatch):
    from tools import sandbox_config

    repo = (tmp_path / "repo").resolve()
    worktree, _ = linked(repo, "one")
    assert list_repo_worktrees(repo) == [worktree]

    def unexpected(*args, **kwargs):
        raise AssertionError("unchanged proof was re-run")

    monkeypatch.setattr(sandbox_config, "verified_worktree_marker", unexpected)
    assert list_repo_worktrees(repo) == [worktree]


def test_sibling_addition_only_proves_new_entry(tmp_path, monkeypatch):
    from tools import sandbox_config

    repo = (tmp_path / "repo").resolve()
    first, _ = linked(repo, "one")
    assert list_repo_worktrees(repo) == [first]
    second, _ = linked(repo, "two")
    original = sandbox_config.verified_worktree_marker
    calls = []

    def record(entry, root):
        calls.append(entry)
        return original(entry, root)

    monkeypatch.setattr(sandbox_config, "verified_worktree_marker", record)
    assert list_repo_worktrees(repo) == [first, second]
    assert calls == [second]


@pytest.mark.parametrize("change", ["remove", "replace"])
def test_sibling_removal_or_replacement_keeps_other_proof(tmp_path, monkeypatch, change):
    from tools import sandbox_config

    repo = (tmp_path / "repo").resolve()
    first, _ = linked(repo, "one")
    second, _ = linked(repo, "two")
    assert list_repo_worktrees(repo) == [first, second]
    shutil.rmtree(second)
    if change == "replace":
        second.mkdir()
    original = sandbox_config.verified_worktree_marker
    calls = []

    def record(entry, root):
        calls.append(entry)
        return original(entry, root)

    monkeypatch.setattr(sandbox_config, "verified_worktree_marker", record)
    assert list_repo_worktrees(repo) == [first]
    assert first not in calls


@pytest.mark.parametrize("replacement", [False, True])
def test_completed_same_size_same_mtime_marker_rewrite_is_seen(tmp_path, replacement):
    repo = (tmp_path / "repo").resolve()
    worktree, _ = linked(repo, "one")
    marker = worktree / ".git"
    assert list_repo_worktrees(repo) == [worktree]
    original = marker.stat()
    target = marker.with_name("new-marker") if replacement else marker
    target.write_text("x" * original.st_size)
    os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))
    if replacement:
        target.replace(marker)
    assert marker.stat().st_size == original.st_size
    assert marker.stat().st_mtime_ns == original.st_mtime_ns
    assert list_repo_worktrees(repo) == []


def test_marker_rewrite_during_signature_collection_is_seen_next_call(tmp_path, monkeypatch):
    repo = (tmp_path / "repo").resolve()
    worktree, _ = linked(repo, "one")
    assert list_repo_worktrees(repo) == [worktree]
    marker = worktree / ".git"
    original = type(marker).lstat
    changed = False

    def rewrite_after_stat(path):
        nonlocal changed
        result = original(path)
        if path == marker and not changed:
            changed = True
            marker.write_text("gitdir: /invalid\n")
        return result

    monkeypatch.setattr(type(marker), "lstat", rewrite_after_stat)
    # A mutation racing an in-flight listing has the verifier's existing TOCTOU boundary.
    assert list_repo_worktrees(repo) == [worktree]
    assert list_repo_worktrees(repo) == []


def test_intermediate_symlink_retarget_is_reverified(tmp_path):
    repo = (tmp_path / "repo").resolve()
    worktree, gitdir = linked(repo, "one")
    alternate = repo / ".git" / "worktrees" / "alternate"
    alternate.mkdir()
    (alternate / "gitdir").write_text(f"{worktree / '.git'}\n")
    alias = repo / ".git" / "worktrees" / "alias"
    alias.symlink_to(gitdir, target_is_directory=True)
    (worktree / ".git").write_text(f"gitdir: {alias}\n")
    assert list_repo_worktrees(repo) == [worktree]
    alias.unlink()
    alias.symlink_to(alternate, target_is_directory=True)
    (alternate / "gitdir").write_text(f"{repo / 'other' / '.git'}\n")
    assert list_repo_worktrees(repo) == []
