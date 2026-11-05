"""The shared backend Git options, proven against real Git in temporary repositories.

Each test plants something a repository's own config can name (a hook, a filesystem
monitor, the ext transport), runs plain ``git`` first as the control that proves the
plant fires, then runs the same command through the shared options and expects
silence.
"""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from infra.backend_git import GIT_CONFIG_ARGS, git_argv
from infra.project_repo_policy import _repo_is_clean, _run_branch_mutation, repo_checkout_state
from infra.worktree_reaper_probe import git as reaper_git
from tools.worktrees import _git as worktrees_git


@pytest.fixture(autouse=True)
def _isolated_git_environment(tmp_path, monkeypatch):
    """Keep the person's own Git config and identity out of every control."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("GIT_ALLOW_PROTOCOL", raising=False)
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "test@example.invalid")


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30, check=True,
    )
    return done.stdout


def _marker_script(path: Path, marker: Path) -> Path:
    path.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    path.chmod(0o755)
    return path


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "tracked").write_text("one\n")
    _git(repo, "add", "tracked")
    _git(repo, "commit", "-qm", "base")
    return repo


def _plant_hook(repo: Path, tmp_path: Path) -> Path:
    marker = tmp_path / "hook-ran"
    _marker_script(repo / ".git" / "hooks" / "post-checkout", marker)
    return marker


def _plant_fsmonitor(repo: Path, tmp_path: Path) -> Path:
    marker = tmp_path / "fsmonitor-ran"
    _git(repo, "config", "core.fsmonitor", str(_marker_script(tmp_path / "monitor", marker)))
    (repo / "tracked").write_text("two\n")
    return marker


def _control_fires(marker: Path, *argv: str, cwd: Path) -> None:
    """Run plain Git and require the plant to fire; then clear it for the measured call."""
    marker.unlink(missing_ok=True)
    subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=30)
    assert marker.exists(), "control did not exercise the planted program"
    marker.unlink()


@pytest.mark.parametrize(
    ("plant", "command"),
    [
        (_plant_hook, lambda name: ("checkout", "-q", "-b", name)),
        (_plant_hook, lambda name: ("worktree", "add", "-q", "-b", name, f"../{name}")),
        (_plant_fsmonitor, lambda name: ("status", "--porcelain")),
        (_plant_fsmonitor, lambda name: ("diff", "--no-ext-diff", "--no-textconv", "--stat")),
    ],
)
def test_shared_set_stops_what_plain_git_runs(tmp_path, plant, command):
    repo = _repo(tmp_path)
    marker = plant(repo, tmp_path)
    marker.unlink(missing_ok=True)
    _control_fires(marker, "git", *command("control"), cwd=repo)

    subprocess.run(git_argv(*command("measured")), cwd=repo, capture_output=True, text=True, timeout=30)

    assert not marker.exists()


def test_shared_set_stops_the_ext_transport_a_repository_enables(tmp_path):
    repo = _repo(tmp_path)
    marker = tmp_path / "ext-ran"
    _git(repo, "config", "protocol.ext.allow", "always")
    url = f"ext::{_marker_script(tmp_path / 'transport', marker)}"
    _control_fires(marker, "git", "ls-remote", url, cwd=repo)

    done = subprocess.run(
        git_argv("ls-remote", url), cwd=repo, capture_output=True, text=True, timeout=30,
    )

    assert not marker.exists()
    assert done.returncode != 0


@pytest.mark.parametrize(
    ("key", "hostile", "expected"),
    [
        ("core.hooksPath", "/tmp", "/dev/null"),
        ("core.fsmonitor", "true", "false"),
        ("protocol.ext.allow", "always", "never"),
        ("submodule.recurse", "true", "false"),
    ],
)
def test_each_shared_option_beats_the_repository_value(tmp_path, key, hostile, expected):
    repo = _repo(tmp_path)
    _git(repo, "config", key, hostile)
    assert _git(repo, "config", "--get", key).strip() == hostile

    assert subprocess.run(
        git_argv("config", "--get", key), cwd=repo, capture_output=True, text=True, timeout=30,
    ).stdout.strip() == expected


def test_shared_set_is_exactly_the_four_options():
    pairs = [GIT_CONFIG_ARGS[i + 1] for i in range(0, len(GIT_CONFIG_ARGS), 2)]
    assert GIT_CONFIG_ARGS[::2] == ("-c",) * 4
    assert pairs == [
        "core.hooksPath=/dev/null",
        "core.fsmonitor=false",
        "protocol.ext.allow=never",
        "submodule.recurse=false",
    ]
    assert git_argv("status") == ["git", *GIT_CONFIG_ARGS, "status"]


def test_reviewer_reader_keeps_the_same_options():
    from reviewer import reviewer_read

    assert reviewer_read._GIT_CONFIG_ARGS == GIT_CONFIG_ARGS
    command = reviewer_read.git_argv("/tmp/g", "a" * 40, "b" * 40, "stat", [])
    assert command[:2 + len(GIT_CONFIG_ARGS)] == ["git", "--git-dir=/tmp/g", *GIT_CONFIG_ARGS]


def test_project_repo_runner_runs_no_hook(tmp_path):
    repo = _repo(tmp_path)
    marker = _plant_hook(repo, tmp_path)
    _control_fires(marker, "git", "checkout", "-q", "-b", "control", cwd=repo)
    _git(repo, "checkout", "-q", "main")
    marker.unlink(missing_ok=True)

    _run_branch_mutation(repo, ["checkout", "-q", "-b", "side"], operation="switch")

    assert _git(repo, "branch", "--show-current").strip() == "side"
    assert not marker.exists()


def test_project_repo_status_runs_no_filesystem_monitor(tmp_path):
    repo = _repo(tmp_path)
    marker = _plant_fsmonitor(repo, tmp_path)
    _control_fires(marker, "git", "status", "--porcelain", cwd=repo)

    state = repo_checkout_state(repo)
    clean = _repo_is_clean(repo)

    assert state.branch == "main"
    assert state.clean is False
    assert clean is False
    assert not marker.exists()


def test_worktree_runner_runs_no_hook(tmp_path):
    repo = _repo(tmp_path)
    marker = _plant_hook(repo, tmp_path)
    _control_fires(marker, "git", "worktree", "add", "-q", "-b", "control", "../control", cwd=repo)
    marker.unlink(missing_ok=True)
    linked = tmp_path / "linked"

    worktrees_git(repo, ["worktree", "add", "-q", "-b", "side", str(linked)], operation="add")

    assert (linked / "tracked").exists()
    assert not marker.exists()


def test_worktree_runner_runs_no_filesystem_monitor(tmp_path):
    repo = _repo(tmp_path)
    marker = _plant_fsmonitor(repo, tmp_path)
    _control_fires(marker, "git", "status", "--porcelain", cwd=repo)

    worktrees_git(repo, ["status", "--porcelain"], operation="status")

    assert not marker.exists()


def test_reaper_probe_runs_no_hook_or_filesystem_monitor(tmp_path):
    repo = _repo(tmp_path)
    hook = _plant_hook(repo, tmp_path)
    monitor = _plant_fsmonitor(repo, tmp_path)
    _control_fires(hook, "git", "--no-optional-locks", "checkout", "-q", "-b", "control", cwd=repo)
    _control_fires(monitor, "git", "--no-optional-locks", "status", "--porcelain", cwd=repo)
    _git(repo, "checkout", "-q", "main")
    hook.unlink(missing_ok=True)
    monitor.unlink(missing_ok=True)

    reaper_git(repo, ["checkout", "-q", "-b", "side"])
    reaper_git(repo, ["status", "--porcelain"])

    assert not hook.exists()
    assert not monitor.exists()


def test_dirty_submodule_still_reads_dirty(tmp_path):
    inner = tmp_path / "inner"
    inner.mkdir()
    _git(inner, "init", "-q", "-b", "main")
    (inner / "file").write_text("one\n")
    _git(inner, "add", "file")
    _git(inner, "commit", "-qm", "inner")
    repo = _repo(tmp_path)
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner), "sub")
    _git(repo, "commit", "-qm", "add submodule")
    assert _repo_is_clean(repo) is True
    (repo / "sub" / "file").write_text("uncommitted\n")

    assert _repo_is_clean(repo) is False
    assert repo_checkout_state(repo).clean is False
