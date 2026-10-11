"""The per-checkout Git answer cache is reused only while its inputs are unchanged."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from infra import git_state_cache
from infra import project_repo_policy as policy_module
from infra.git_state_cache import (
    cached_current_branch,
    cached_git_answer,
    cached_layout_error,
    clear_git_state_cache,
)
from infra.project_repo_policy import branch_pin_support_error, current_repo_branch


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Isolate the global/system config so the key and the answers are
    # deterministic on any host.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    path = (tmp_path / "repo").resolve()
    subprocess.run(
        ["git", "-c", "init.defaultRefFormat=files", "init", "-b", "main", str(path)],
        check=True, capture_output=True, text=True,
    )
    _git(path, "commit", "--allow-empty", "-m", "init")
    clear_git_state_cache()
    return path


@pytest.fixture
def git_runs(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    runs: list[list[str]] = []
    real_run = subprocess.run

    def counting_run(argv, *args, **kwargs):
        if argv and argv[0] == "git":
            runs.append(list(argv))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counting_run)
    return runs


def _branch(repo: Path) -> str:
    return cached_current_branch(repo, current_repo_branch)


def _layout(repo: Path) -> str | None:
    return cached_layout_error(repo, policy_module._git_layout_support_error, repo / ".git")


def test_unchanged_inputs_reuse_the_answer_without_running_git(repo, git_runs) -> None:
    assert _branch(repo) == "main"
    git_runs.clear()
    assert _branch(repo) == "main"
    assert git_runs == []
    assert _layout(repo) is None
    git_runs.clear()
    assert _layout(repo) is None
    assert git_runs == []


def test_head_branch_switch_is_seen(repo) -> None:
    assert _branch(repo) == "main"
    _git(repo, "checkout", "-b", "other")
    assert _branch(repo) == "other"


def test_in_place_head_rewrite_with_restored_mtime_is_seen(repo) -> None:
    head = repo / ".git" / "HEAD"
    _git(repo, "branch", "mmmm")
    head.write_text("ref: refs/heads/main\n", encoding="utf-8")
    assert _branch(repo) == "main"
    before = head.stat()
    head.write_text("ref: refs/heads/mmmm\n", encoding="utf-8")  # same size
    os.utime(head, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert head.stat().st_size == before.st_size
    assert _branch(repo) == "mmmm"


def test_detached_head_is_never_cached_as_a_branch(repo) -> None:
    assert _branch(repo) == "main"
    _git(repo, "checkout", "--detach")
    with pytest.raises(ValueError):
        _branch(repo)


def test_config_edit_flips_the_layout_answer(repo) -> None:
    config = repo / ".git" / "config"
    original = config.read_text(encoding="utf-8")
    assert _layout(repo) is None
    config.write_text(original + "[extensions]\n\trefStorage = reftable\n", encoding="utf-8")
    changed = _layout(repo)
    assert changed == policy_module._git_layout_support_error(repo, repo / ".git")
    assert changed is not None and "reftable" in changed
    config.write_text(original, encoding="utf-8")
    assert _layout(repo) is None


def test_included_config_edit_is_seen(repo, tmp_path) -> None:
    included = tmp_path / "extra.cfg"
    included.write_text("[core]\n\tfileMode = true\n", encoding="utf-8")
    _git(repo, "config", "include.path", str(included))
    assert _layout(repo) is None
    included.write_text("[extensions]\n\trefStorage = reftable\n", encoding="utf-8")
    changed = _layout(repo)
    assert changed == policy_module._git_layout_support_error(repo, repo / ".git")
    assert changed is not None


def test_global_config_creation_and_edit_is_seen(repo, git_runs) -> None:
    assert _layout(repo) is None
    global_config = Path(os.environ["HOME"]) / ".gitconfig"
    global_config.write_text("[core]\n\teditor = true\n", encoding="utf-8")
    git_runs.clear()
    assert _layout(repo) is None
    assert git_runs, "a new global config file must invalidate the cached answer"
    git_runs.clear()
    assert _layout(repo) is None
    assert git_runs == []
    global_config.write_text("[core]\n\teditor = false\n", encoding="utf-8")
    assert _layout(repo) is None
    assert git_runs, "an edited global config file must invalidate the cached answer"


def test_commondir_or_reftable_appearing_is_seen(repo) -> None:
    assert _layout(repo) is None
    (repo / ".git" / "commondir").write_text("../elsewhere\n", encoding="utf-8")
    assert _layout(repo) == policy_module._git_layout_support_error(repo, repo / ".git")
    assert branch_pin_support_error(repo) is not None


def test_git_environment_override_changes_the_key(repo, monkeypatch, git_runs) -> None:
    assert _branch(repo) == "main"
    git_runs.clear()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(repo.parent))
    assert _branch(repo) == "main"
    assert git_runs, "a Git environment change must recompute"


def test_denials_and_errors_are_recomputed(repo, git_runs) -> None:
    (repo / ".git" / "commondir").write_text("../elsewhere\n", encoding="utf-8")
    first = _layout(repo)
    assert first is not None
    git_runs.clear()
    assert _layout(repo) == first
    assert git_runs, "a denial must never be served from the cache"


def test_symlinked_input_is_not_cached(repo, tmp_path, git_runs) -> None:
    config = repo / ".git" / "config"
    real = tmp_path / "config-copy"
    real.write_text(config.read_text(encoding="utf-8"), encoding="utf-8")
    config.unlink()
    config.symlink_to(real)
    assert _branch(repo) == "main"
    git_runs.clear()
    assert _branch(repo) == "main"
    assert git_runs, "a symlinked config is not a closed input set"


def test_linked_worktree_checkout_is_not_cached(repo, git_runs) -> None:
    worktree = repo / ".worktrees" / "wt"
    _git(repo, "worktree", "add", "-b", "wt-branch", str(worktree))
    assert _branch(worktree) == "wt-branch"
    git_runs.clear()
    assert _branch(worktree) == "wt-branch"
    assert git_runs, "a gitfile checkout has no keyed HEAD, so it is computed fresh"


def test_cache_is_lru_bounded(tmp_path, repo, monkeypatch) -> None:
    monkeypatch.setattr(git_state_cache, "_MAX_ENTRIES", 2)
    for index in range(4):
        _branch(repo)
        cached_git_answer(repo, f"other-{index}", lambda: "x")
    assert len(git_state_cache._entries) <= 2


def test_tag_named_like_the_branch_changes_the_abbreviation(repo) -> None:
    assert _branch(repo) == "main"
    _git(repo, "tag", "main")
    assert _branch(repo) == current_repo_branch(repo) == "heads/main"
    _git(repo, "tag", "-d", "main")
    assert _branch(repo) == "main"


def test_packed_tag_and_packed_deletion_are_seen(repo) -> None:
    assert _branch(repo) == "main"
    _git(repo, "tag", "main")
    _git(repo, "pack-refs", "--all")  # the loose tag file is gone
    assert not (repo / ".git" / "refs" / "tags" / "main").exists()
    assert _branch(repo) == current_repo_branch(repo) == "heads/main"
    _git(repo, "tag", "-d", "main")
    assert _branch(repo) == current_repo_branch(repo) == "main"


@pytest.mark.parametrize(
    "ref", ["refs/remotes/main", "refs/remotes/main/HEAD", "refs/main", "refs/tags/main"],
)
def test_every_shadowing_ref_location_is_seen(repo, ref) -> None:
    assert _branch(repo) == "main"
    _git(repo, "update-ref", ref, "HEAD")
    assert _branch(repo) == current_repo_branch(repo)


def test_slashed_branch_shadowed_by_a_tag_is_seen(repo) -> None:
    _git(repo, "checkout", "-b", "feature/x")
    assert _branch(repo) == "feature/x"
    _git(repo, "tag", "feature/x")
    assert _branch(repo) == current_repo_branch(repo) == "heads/feature/x"


def test_routine_git_activity_does_not_invalidate(repo, git_runs) -> None:
    assert _branch(repo) == "main"
    assert _layout(repo) is None
    (repo / "file.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "status")  # refreshes the index through index.lock
    _git(repo, "hash-object", "-w", "file.txt")  # writes a loose object
    _git(repo, "branch", "side")  # writes under refs/heads
    git_runs.clear()
    assert _branch(repo) == "main"
    assert _layout(repo) is None
    assert git_runs == []


def test_core_worktree_is_never_cached(repo, git_runs) -> None:
    config = repo / ".git" / "config"
    config.write_text(config.read_text(encoding="utf-8") + "[core]\n\tworktree = " + str(repo) + "\n", encoding="utf-8")
    assert _layout(repo) == policy_module._git_layout_support_error(repo, repo / ".git")
    git_runs.clear()
    _layout(repo)
    assert git_runs
