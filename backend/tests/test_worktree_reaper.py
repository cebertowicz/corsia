"""Worktree reaper: removal, every skip reason, scope, callers and metric.

Every test builds a real temporary Git repository with real linked worktrees;
only the process probe (lsof is unusable inside agent sandboxes) and the
active-turn read are substituted, and the real lsof path has its own tests.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
from typing import Any

import pytest

from infra import (
    repo_flock,
    worktree_reaper,
    worktree_reaper_checks,
    worktree_reaper_probe,
    worktree_reaper_remove,
    worktree_reaper_scan,
    worktree_reaper_store,
)
import infra.worktree_reaper_pr as worktree_reaper_pr
from infra.project_repo_policy import RepoPolicy
from infra.worktree_reaper import RepoReport, emit_report, reap
from infra.worktree_reaper_probe import ProcessProbe
from tools.sandbox_config import list_repo_worktrees

UUID = "0123abcd-1111-2222-3333-444455556666"
DAY = 86400


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


class FakeProbe(ProcessProbe):
    """Process probe with scripted answers; everything else is real."""

    def __init__(self, reasons: dict[str, str] | None = None) -> None:
        super().__init__()
        self.reasons = reasons or {}

    def refresh(self) -> None:
        return None

    def skip_reason(self, worktree: Path) -> str | None:
        return self.reasons.get(worktree.name)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(repo_flock, "_lock_dir", lambda: state / "locks")
    monkeypatch.setattr(
        worktree_reaper_store, "_store_path", lambda: state / "reaper" / "pr.json",
    )
    monkeypatch.setattr(worktree_reaper_checks, "_load_active_threads", lambda: set())
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        worktree_reaper, "emit_product_event",
        lambda category, action, *, properties: events.append(
            {"event": f"{category}.{action}", **properties}),
    )
    return events


@pytest.fixture
def repo(tmp_path) -> Path:
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / ".gitignore").write_text("node_modules/\n.env\n")
    (root / "README").write_text("hi\n")
    (root / "ro").mkdir()
    (root / "ro" / "kept").write_text("x\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    _git(root, "remote", "add", "origin", "git@github.com:Owner/Repo.git")
    return root


def _policy(repo: Path) -> RepoPolicy:
    return RepoPolicy(
        project_id="p", repo_name="repo", path=repo,
        pinned_branch=None, protect_base_checkout=False,
    )


def _add(repo: Path, name: str, branch: str | None = None) -> Path:
    _git(repo, "worktree", "add", "-q", "-b", branch or f"b-{name}", f".worktrees/{name}")
    return repo / ".worktrees" / name


def test_named_github_remote_preserves_origin_normalization(repo):
    """Named GitHub remotes share origin's normalized owner/repository form."""
    _git(repo, "remote", "add", "upstream", "git@github.com:Upstream/Repository.git")

    assert worktree_reaper_probe.github_remote_full_name(repo, "upstream") == "upstream/repository"
    assert worktree_reaper_probe.origin_full_name(repo) == "owner/repo"


def test_non_github_named_remote_is_ignored(repo):
    """Only GitHub URLs contribute a repository name for any remote."""
    _git(repo, "remote", "add", "upstream", "https://gitlab.com/owner/repo.git")

    assert worktree_reaper_probe.github_remote_full_name(repo, "upstream") is None


def _admin(worktree: Path) -> Path:
    admin = worktree_reaper_probe.admin_dir(worktree)
    assert admin is not None
    return admin


def _age(repo: Path, worktree: Path, seconds: float) -> None:
    stamp = time.time() - seconds
    admin = _admin(worktree)
    branch = (admin / "HEAD").read_text().strip().removeprefix("ref: refs/heads/")
    for path in (admin / "HEAD", admin / "index", admin / "logs" / "HEAD",
                 admin / "ORIG_HEAD", repo / ".git" / "logs" / "refs" / "heads" / branch,
                 worktree):
        if path.exists():
            os.utime(path, (stamp, stamp))


def _age_tree(root: Path, seconds: float) -> None:
    stamp = time.time() - seconds
    for current, dirs, files in os.walk(root, topdown=False):
        for name in (*dirs, *files):
            os.utime(Path(current) / name, (stamp, stamp), follow_symlinks=False)
    os.utime(root, (stamp, stamp))


def _run(repo: Path, probe: ProcessProbe | None = None, **kwargs) -> RepoReport:
    return reap(_policy(repo), mode="idle", probe=probe or FakeProbe(), **kwargs)


def _reasons(report: RepoReport) -> dict[str, str]:
    return {item.name: item.reason for item in report.skips}


def _quarantine(repo: Path) -> list[str]:
    root = worktree_reaper_remove.quarantine_root(repo)
    return sorted(p.name for p in root.iterdir()) if root.exists() else []


# --- removal ---------------------------------------------------------------

def test_clean_idle_worktree_is_removed_and_its_branch_survives(repo):
    wt = _add(repo, "clean", "keep-me")
    _age(repo, wt, 8 * DAY)

    report = _run(repo)

    assert report.removed == 1 and report.skips == []
    assert not wt.exists()
    assert _git(repo, "rev-parse", "--verify", "refs/heads/keep-me")
    assert wt not in list_repo_worktrees(repo)
    assert "keep-me" not in _git(repo, "worktree", "list", "--porcelain")
    assert _quarantine(repo) == []


@pytest.mark.parametrize("directory", ["node_modules", ".mypy_cache", ".ruff_cache"])
def test_allowlisted_ignored_directory_does_not_block_removal(repo, directory):
    (repo / ".git" / "info" / "exclude").write_text(f"{directory}/\n")
    wt = _add(repo, "deps")
    (wt / directory / "pkg").mkdir(parents=True)
    (wt / directory / "pkg" / "index.js").write_text("x")
    _age(repo, wt, 8 * DAY)

    assert _run(repo).removed == 1
    assert not wt.exists()


def _exclude(repo: Path, *patterns: str) -> None:
    path = repo / ".git" / "info" / "exclude"
    path.write_text("".join(f"{pattern}\n" for pattern in patterns))


@pytest.mark.parametrize(
    ("path", "pattern"),
    [
        (".check-pr-mypy-venv/site", ".check-pr-*/"),
        (".check-pr-speed-gate/site", ".check-pr-*/"),
        ("frontend/static/build-info.json", "frontend/static/build-info.json"),
        ("frontend/src/lib/build-info.json", "frontend/src/lib/build-info.json"),
    ],
)
def test_gate_scratch_and_generated_build_info_are_disposable(repo, path, pattern):
    _exclude(repo, pattern)
    wt = _add(repo, "generated")
    target = wt / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("generated\n")

    assert worktree_reaper_probe.status_skip_reason(wt) is None


@pytest.mark.parametrize("name", ["node_modules", ".venv"])
def test_symlinked_dependency_entry_is_disposable(repo, tmp_path, name):
    _exclude(repo, name)
    wt = _add(repo, "symlink")
    target = tmp_path / f"{name}-target"
    target.mkdir()
    (wt / name).symlink_to(target, target_is_directory=True)

    status = _git(wt, "status", "--porcelain=v2", "--ignored=matching")

    assert f"! {name}" in status
    assert worktree_reaper_probe.status_skip_reason(wt) is None


@pytest.mark.parametrize(
    ("path", "pattern", "contents"),
    [
        (".qa/", ".qa/", "state"),
        ("frontend/.qa/", "frontend/.qa/", "state"),
        (".diagnosis/x/state/corsia.db", ".diagnosis/x/state/corsia.db", "state"),
        (".env", ".env", "state"),
        ("frontend/node_modules.bak", "frontend/node_modules.bak", "state"),
    ],
)
def test_non_allowlisted_ignored_paths_remain_data(repo, path, pattern, contents):
    _exclude(repo, pattern)
    wt = _add(repo, "kept")
    target = wt / path
    if path.endswith("/"):
        target.mkdir(parents=True)
        (target / contents).write_text("kept\n")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{contents}\n")

    assert worktree_reaper_probe.status_skip_reason(wt) == "ignored_data"


# --- each skip reason -------------------------------------------------------

def _prepare(repo: Path, kind: str) -> Path:
    wt = _add(repo, kind)
    if kind == "locked":
        _git(repo, "worktree", "lock", str(wt))
    elif kind == "git_lock":
        (_admin(wt) / "index.lock").write_text("")
    elif kind == "dirty":
        (wt / "README").write_text("changed\n")
    elif kind == "untracked":
        (wt / "notes.txt").write_text("mine\n")
    elif kind == "ignored_data":
        (wt / ".env").write_text("SECRET=1\n")
    if kind != "recent":
        _age(repo, wt, (3 if kind in {"dirty", "untracked", "ignored_data"} else 8) * DAY)
    return wt


@pytest.mark.parametrize(
    "kind", ["locked", "git_lock", "recent", "dirty", "untracked", "ignored_data"],
)
def test_each_unsafe_state_is_skipped_with_its_reason(repo, kind):
    wt = _prepare(repo, kind)

    report = _run(repo)

    assert report.removed == 0
    assert _reasons(report) == {kind: kind}
    assert wt.is_dir()
    if kind == "git_lock":
        assert (_admin(wt) / "index.lock").exists()


def test_live_process_skip_reason_is_recorded(repo):
    wt = _add(repo, "busy")
    _age(repo, wt, 8 * DAY)

    report = _run(repo, FakeProbe({"busy": "in_use"}))

    assert _reasons(report) == {"busy": "in_use"} and wt.is_dir()


def test_owning_thread_with_a_running_turn_is_skipped(repo, monkeypatch):
    wt = _add(repo, f"{UUID}-task")
    short = _add(repo, "0123abcd-other")
    for path in (wt, short):
        _age(repo, path, 8 * DAY)
    monkeypatch.setattr(worktree_reaper_checks, "_load_active_threads", lambda: {UUID})

    report = _run(repo)

    assert _reasons(report) == {
        f"{UUID}-task": "thread_active", "0123abcd-other": "thread_active",
    }
    assert {item.owner_thread for item in report.skips} == {UUID, "0123abcd"}


def test_unreadable_turn_state_fails_closed(repo, monkeypatch):
    wt = _add(repo, f"{UUID}-task")
    _age(repo, wt, 8 * DAY)

    def _broken() -> set[str]:
        raise sqlite3.OperationalError("db locked")

    monkeypatch.setattr(worktree_reaper_checks, "_load_active_threads", _broken)

    assert _reasons(_run(repo)) == {f"{UUID}-task": "thread_state_unknown"}


# --- real process probe -----------------------------------------------------

def _lsof_usable() -> bool:
    return ProcessProbe()._snapshot() is not None


@pytest.mark.skipif(not _lsof_usable(), reason="lsof cannot list processes here")
def test_real_sleep_process_with_cwd_inside_blocks_removal(repo):
    wt = _add(repo, "sleeper")
    _age(repo, wt, 8 * DAY)
    sleeper = subprocess.Popen(["sleep", "30"], cwd=wt)
    try:
        report = _run(repo, ProcessProbe())
    finally:
        sleeper.kill()
        sleeper.wait()
    assert _reasons(report) == {"sleeper": "in_use"} and wt.is_dir()


def _fake_lsof(monkeypatch, responses: dict[str, subprocess.CompletedProcess[str] | None]):
    def _lsof(args: list[str]):
        return responses["cwd" if "cwd" in args else "open"]

    monkeypatch.setattr(worktree_reaper_probe, "_lsof", _lsof)


def _done(code: int, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, out, err)


def test_process_probe_matches_a_cwd_inside_the_worktree(repo, monkeypatch):
    wt = _add(repo, "cwd")
    _fake_lsof(monkeypatch, {"cwd": _done(0, f"p42\nfcwd\nn{wt}/src\n"), "open": _done(1)})
    assert ProcessProbe().skip_reason(wt) == "in_use"
    _fake_lsof(monkeypatch, {"cwd": _done(0, f"p42\nfcwd\nn{wt}-other\n"), "open": _done(1)})
    assert ProcessProbe().skip_reason(wt) is None
    _fake_lsof(monkeypatch, {"cwd": _done(0, "p1\nn/\n"), "open": _done(0, "42\n")})
    assert ProcessProbe().skip_reason(wt) == "in_use"


def test_missing_or_failing_lsof_fails_closed(repo, monkeypatch):
    wt = _add(repo, "nolsof")
    _age(repo, wt, 8 * DAY)
    monkeypatch.setattr(worktree_reaper_probe.shutil, "which", lambda _name: None)
    monkeypatch.setattr(worktree_reaper_probe.os.path, "exists", lambda _path: False)

    assert _reasons(_run(repo, ProcessProbe())) == {"nolsof": "unknown_process"}

    _fake_lsof(monkeypatch, {"cwd": _done(1, "", "lsof: permission denied"), "open": None})
    assert ProcessProbe().skip_reason(wt) == "unknown_process"
    _fake_lsof(monkeypatch, {"cwd": _done(0, ""), "open": _done(1, "", "lsof: WARNING")})
    assert ProcessProbe().skip_reason(wt) == "unknown_process"
    assert wt.is_dir()


# --- scope -----------------------------------------------------------------

def test_only_entries_under_dot_worktrees_are_candidates(repo, tmp_path, monkeypatch):
    outside = tmp_path / "serving"
    _git(repo, "worktree", "add", "-q", "-b", "serving", str(outside))
    own = _add(repo, "backend-own")
    for path in (outside, own):
        _age(repo, path, 8 * DAY)
    monkeypatch.setattr(worktree_reaper_scan, "_BACKEND_FILE", own / "backend" / "x.py")

    report = _run(repo)

    assert report.scanned == 0 and report.removed == 0
    assert outside.is_dir() and own.is_dir() and (repo / "README").exists()
    assert str(repo) in _git(repo, "worktree", "list")


# --- idle clock ------------------------------------------------------------

def test_the_reapers_own_probes_do_not_move_the_idle_clock(repo, monkeypatch):
    wt = _add(repo, f"{UUID}-idle")
    (wt / "README").touch()  # stale index stat info invites a refresh
    _age(repo, wt, 8 * DAY)
    before = worktree_reaper_scan.idle_since(wt, _admin(wt))
    monkeypatch.setattr(worktree_reaper_checks, "_load_active_threads", lambda: {UUID})

    report = _run(repo)

    assert _reasons(report) == {f"{UUID}-idle": "thread_active"}  # status ran
    assert worktree_reaper_scan.idle_since(wt, _admin(wt)) == before


def test_gc_rewriting_every_reflog_does_not_restart_the_idle_clock(repo):
    wt = _add(repo, f"{UUID}-idle")
    _age(repo, wt, 8 * DAY)

    _git(repo, "reflog", "expire", "--all", "--expire=never")  # what `git gc` runs

    assert time.time() - (_admin(wt) / "logs" / "HEAD").stat().st_mtime < DAY  # rewritten
    assert time.time() - worktree_reaper_scan.idle_since(wt, _admin(wt)) > 7 * DAY
    report = _run(repo)
    assert report.removed == 1 and report.skips == []
    assert not wt.exists()


# --- PR mapping ------------------------------------------------------------

def _merged(repo: Path, branch: str, sha: str, *, fork: bool = False) -> dict[str, Any]:
    head_repo = "someone/Repo" if fork else "Owner/Repo"
    return {
        "repository": {"full_name": "Owner/Repo"},
        "pull_request": {"merged": True, "head": {
            "ref": branch, "sha": sha, "repo": {"full_name": head_repo}}},
    }


def test_merged_pr_reaps_only_its_own_worktree(repo, monkeypatch):
    mine = _add(repo, "mine", "feature")
    other = _add(repo, "other", "unrelated")
    for path in (mine, other):
        _age(repo, path, 2 * 3600)
    monkeypatch.setattr(worktree_reaper, "ProcessProbe", FakeProbe)
    sha = _git(mine, "rev-parse", "HEAD")

    report = worktree_reaper_pr.reap_merged_pr(
        _merged(repo, "feature", sha), policies=[_policy(repo)],
    )

    assert report.trigger == "pr_terminal" and report.removed == 1
    assert not mine.exists() and other.is_dir()


def test_fork_pr_is_ignored(repo):
    mine = _add(repo, "mine", "feature")
    sha = _git(mine, "rev-parse", "HEAD")
    report = worktree_reaper_pr.reap_merged_pr(
        _merged(repo, "feature", sha, fork=True), policies=[_policy(repo)],
    )
    assert _reasons(report) == {"feature": "fork"} and mine.is_dir()


def test_local_commits_after_the_pr_head_are_kept(repo):
    mine = _add(repo, "mine", "feature")
    pr_sha = _git(mine, "rev-parse", "HEAD")
    (mine / "later.txt").write_text("x")
    _git(mine, "add", "later.txt")
    _git(mine, "commit", "-q", "-m", "after push")
    report = worktree_reaper_pr.reap_merged_pr(
        _merged(repo, "feature", pr_sha), policies=[_policy(repo)],
    )
    assert _reasons(report) == {"mine": "ahead_of_pr"} and mine.is_dir()


def test_merged_pr_worktree_is_removed_at_merge_despite_scratch_and_a_running_turn(
    repo, monkeypatch,
):
    # The owner merges from inside its own turn, seconds after its last edit,
    # leaving tool caches and a PR-body draft behind: none of that keeps it.
    (repo / ".git" / "info" / "exclude").write_text("*.db\n")
    mine = _add(repo, f"{UUID}-feature", "feature")
    (mine / ".mypy_cache").mkdir()
    (mine / ".mypy_cache" / "x.json").write_text("x")
    (mine / "seeded.db").write_text("local\n")
    (mine / ".pr-body.md").write_text("draft\n")
    monkeypatch.setattr(worktree_reaper_checks, "_load_active_threads", lambda: {UUID})
    monkeypatch.setattr(worktree_reaper, "ProcessProbe", FakeProbe)
    sha = _git(mine, "rev-parse", "HEAD")

    report = worktree_reaper_pr.reap_merged_pr(
        _merged(repo, "feature", sha), policies=[_policy(repo)],
    )

    assert report.removed == 1 and report.skips == [] and not mine.exists()
    assert worktree_reaper_store.pr_terminal_heads() == {}


def test_merged_worktree_blocked_at_merge_is_retried_by_the_nightly_pass(repo, monkeypatch):
    mine = _add(repo, "mine", "feature")
    plain = _add(repo, "plain", "plain")
    (mine / "README").write_text("uncommitted\n")
    monkeypatch.setattr(worktree_reaper, "ProcessProbe", FakeProbe)
    sha = _git(mine, "rev-parse", "HEAD")

    report = worktree_reaper_pr.reap_merged_pr(
        _merged(repo, "feature", sha), policies=[_policy(repo)],
    )
    assert _reasons(report) == {"mine": "dirty"} and mine.is_dir()
    assert set(worktree_reaper_store.pr_terminal_heads()) == {str(mine)}

    _git(mine, "checkout", "--", "README")
    nightly = _run(repo)  # no aging: a merged worktree has no idle clock
    assert not mine.exists()
    assert _reasons(nightly) == {"plain": "recent"}
    assert worktree_reaper_store.pr_terminal_heads() == {}


def test_merged_worktree_in_use_by_a_live_process_is_kept(repo, monkeypatch):
    mine = _add(repo, "mine", "feature")
    monkeypatch.setattr(worktree_reaper, "ProcessProbe", lambda: FakeProbe({"mine": "in_use"}))
    sha = _git(mine, "rev-parse", "HEAD")

    report = worktree_reaper_pr.reap_merged_pr(
        _merged(repo, "feature", sha), policies=[_policy(repo)],
    )

    assert _reasons(report) == {"mine": "in_use"} and mine.is_dir()


# --- failure paths ---------------------------------------------------------

def test_failed_remove_leaves_only_a_quarantine_entry_that_the_next_run_reaps(repo):
    wt = _add(repo, "readonly")
    _age(repo, wt, 8 * DAY)
    os.chmod(wt / "ro", 0o555)

    first = _run(repo)

    assert _reasons(first) == {"readonly": "quarantine_removal_pending"}
    assert not wt.exists()
    assert len(_quarantine(repo)) == 1
    assert first.quarantine_entries == _quarantine(repo)

    second = _run(repo)

    assert second.quarantine_removed == 1
    assert _quarantine(repo) == [] and second.quarantine_entries == []


def test_file_written_after_the_precheck_moves_the_worktree_back(repo, monkeypatch):
    wt = _add(repo, "racy")
    _age(repo, wt, 8 * DAY)
    real_git = worktree_reaper_remove.git

    def _racing_git(cwd: Path, args: list[str], **kwargs):
        result = real_git(cwd, args, **kwargs)
        into_quarantine = worktree_reaper_remove.QUARANTINE_DIR in args[-1]
        if args[:2] == ["worktree", "move"] and result.returncode == 0 and into_quarantine:
            (Path(args[-1]) / "late.txt").write_text("written mid-removal")
        return result

    monkeypatch.setattr(worktree_reaper_remove, "git", _racing_git)

    report = _run(repo)

    assert _reasons(report) == {"racy": "raced_dirty"}
    assert (wt / "late.txt").exists()
    assert wt in list_repo_worktrees(repo)
    assert _quarantine(repo) == []


def test_busy_repo_lock_skips_the_repository(repo):
    wt = _add(repo, "waiting")
    _age(repo, wt, 8 * DAY)

    with repo_flock.repo_lock(repo):
        report = _run(repo)

    assert _reasons(report) == {"waiting": "repo_busy"} and wt.is_dir()
    assert _run(repo).removed == 1


# --- orphans (unregistered leftovers under .worktrees/) --------------------

def _orphan(repo: Path, name: str, age: float) -> Path:
    path = repo / ".worktrees" / name
    (path / "deep").mkdir(parents=True)
    (path / "deep" / "file").write_text("x")
    _age_tree(path, age)
    return path


def test_idle_unregistered_orphan_is_removed(repo):
    (repo / ".worktrees").mkdir()
    old = _orphan(repo, "half-deleted", 8 * DAY)

    report = _run(repo)

    assert report.orphan_removed == 1 and not old.exists()
    assert _quarantine(repo) == []


def test_orphans_that_fail_any_condition_are_kept(repo):
    (repo / ".worktrees").mkdir()
    recent = _orphan(repo, "recent-inside", 8 * DAY)
    (recent / "deep" / "file").write_text("new")
    marked = _orphan(repo, "has-git", 8 * DAY)
    (marked / ".git").write_text("gitdir: /nowhere\n")
    _age_tree(marked, 8 * DAY)
    busy = _orphan(repo, "busy", 8 * DAY)

    report = _run(repo, FakeProbe({"busy": "in_use"}))

    kept = {item.name: item.detail for item in report.skips}
    assert kept == {
        "recent-inside": "recent", "has-git": "git_entry_present", "busy": "in_use",
    }
    assert {item.reason for item in report.skips} == {"orphan_kept"}
    assert all(path.is_dir() for path in (recent, marked, busy))


# --- callers ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_router_hook_neither_blocks_nor_raises(monkeypatch, isolated):
    from events import router

    release = threading.Event()

    def _slow_then_broken(_payload, **_kwargs):
        release.wait(5)
        raise RuntimeError("reaper exploded")

    monkeypatch.setattr(worktree_reaper_pr, "reap_merged_pr", _slow_then_broken)
    monkeypatch.setattr(router, "record_pull_request_ended", lambda *a, **k: None)

    class _Identity:
        integration, type, external_id = "github", "pull-request", "Owner/Repo#1"

    started = time.monotonic()
    router._record_source_facts(
        # GitHub's real terminal delivery carries `action: "closed"` beside the
        # merged fact; the one rule reads no terminal state without it.
        "webhook.github.pr_merged", _Identity(),
        {"repository": {"full_name": "Owner/Repo"}, "action": "closed",
         "pull_request": {"merged": True}},
    )
    assert time.monotonic() - started < 1.0
    pending = list(worktree_reaper_pr._PENDING)
    assert len(pending) == 1
    release.set()
    await asyncio.gather(*pending)
    assert isolated[-1]["error"] == "pass_error: RuntimeError: reaper exploded"
    assert isolated[-1]["trigger"] == "pr_terminal"


@pytest.fixture
def run_db(tmp_path, monkeypatch):
    path = tmp_path / "nightly-runs.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE fact_extraction_runs (run_date TEXT PRIMARY KEY, "
            "status TEXT NOT NULL DEFAULT 'ok', result_json TEXT, ran_at TEXT NOT NULL)"
        )

    def factory():
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        return db

    from jobs import nightly

    monkeypatch.setattr(nightly, "get_db", factory)
    monkeypatch.setattr("infra.database.get_db", factory)
    # These reaper tests isolate nightly status in a one-table DB; managed-process
    # retirement is an unrelated maintenance stage with its own schema and tests.
    monkeypatch.setattr("automation.managed_process_events.drain_retirements", lambda: ())
    return path


@pytest.mark.asyncio
async def test_nightly_reaper_failure_leaves_retention_status_ok(run_db, monkeypatch):
    from core import thread_lifecycle
    from jobs import nightly

    calls: list[str] = []
    monkeypatch.setattr(thread_lifecycle, "run_lifecycle_retention",
                        lambda: calls.append("retention"))

    def _broken() -> None:
        calls.append("reaper")
        raise RuntimeError("reaper exploded")

    monkeypatch.setattr(worktree_reaper, "reap_all_idle", _broken)

    await nightly._run_job_safely()

    assert calls == ["retention", "reaper"]
    with sqlite3.connect(run_db) as db:
        status = db.execute("SELECT status FROM fact_extraction_runs").fetchone()[0]
    assert status == "ok"


def test_reap_all_idle_covers_only_enforceable_policies(repo, monkeypatch, isolated):
    wt = _add(repo, "clean")
    _age(repo, wt, 8 * DAY)
    broken = RepoPolicy(
        project_id="p", repo_name="gone", path=repo.parent / "gone",
        pinned_branch=None, protect_base_checkout=False, enforcement_error="bad",
    )
    monkeypatch.setattr(worktree_reaper, "list_registered_repo_policies",
                        lambda: [_policy(repo), broken])
    monkeypatch.setattr(worktree_reaper, "ProcessProbe", FakeProbe)

    reports = worktree_reaper.reap_all_idle()

    assert [r.repo for r in reports] == [str(repo)] and not wt.exists()
    assert [event["repo"] for event in isolated] == [str(repo)]


# --- metric ----------------------------------------------------------------

def test_run_metric_payload_shape(repo, isolated):
    wt = _add(repo, f"{UUID}-dirty")
    (wt / "README").write_text("changed\n")
    _age(repo, wt, 8 * DAY)

    emit_report(_run(repo))

    event = isolated[-1]
    assert event["event"] == "worktree_reaper.run_completed"
    assert event["repo"] == str(repo) and event["trigger"] == "idle"
    assert (event["scanned"], event["removed"], event["remaining_count"]) == (1, 0, 1)
    assert event["skip_counts"] == {"pr_state_unknown": 1}
    assert event["oldest_skipped_idle_days"] >= 7.9
    assert event["skipped"] == [{
        "name": f"{UUID}-dirty", "owner_thread": UUID, "reason": "pr_state_unknown",
        "idle_days": event["oldest_skipped_idle_days"],
    }]


def test_run_metric_caps_skipped_items_at_fifty(isolated):
    report = RepoReport(repo="/r", trigger="idle", scanned=60)
    for index in range(60):
        report.skip(f"wt-{index}", None, "recent", 0.1)

    emit_report(report)

    event = isolated[-1]
    assert len(event["skipped"]) == 50 and event["skipped_truncated"] == 10
    assert event["skip_counts"] == {"recent": 60} and event["remaining_count"] == 60


def test_run_metric_lists_what_is_still_quarantined(repo, isolated):
    """A registered entry the sweep must not delete stays visible every run."""
    stuck = _add(repo, "stuck")
    quarantine = worktree_reaper_remove.quarantine_root(repo)
    quarantine.mkdir()
    _git(repo, "worktree", "move", str(stuck), str(quarantine / "stuck-1"))

    for mode in ("idle", "pr_terminal"):
        emit_report(reap(_policy(repo), mode=mode, probe=FakeProbe()))
        event = isolated[-1]
        assert event["trigger"] == mode
        assert event["quarantine_entry_count"] == 1
        assert event["quarantine_entries"] == ["stuck-1"]
        assert event["error"] is None
    assert (quarantine / "stuck-1").is_dir()


# --- hardening: locale, live recheck, pass-level failure ------------------

_GERMAN_DIRTY = "enthält geänderte oder unversionierte Dateien"


def _translating_git(tmp_path: Path, monkeypatch) -> None:
    """Put a git first on PATH that translates like a localized build.

    The host git ships without translations, so this wrapper stands in for
    one: unless LC_ALL is C it rewrites the dirty-tree refusal into German.
    Every other command, and every C-locale call, is the real git unchanged.
    """
    real = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
    shim_dir = tmp_path / "localized-git"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(
        "#!/bin/bash\n"
        f'REAL="{real}"\n'
        'if [ "${LC_ALL:-}" != "C" ] && [[ " $* " == *" worktree remove "* ]]; then\n'
        '  "$REAL" "$@" 2>&1 | sed "s/contains modified or untracked files.*/'
        f'{_GERMAN_DIRTY}/" >&2\n'
        "  exit ${PIPESTATUS[0]}\n"
        "fi\n"
        'exec "$REAL" "$@"\n'
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    monkeypatch.setenv("LANG", "de_DE.UTF-8")


def test_race_is_recognized_under_a_non_english_locale(repo, tmp_path, monkeypatch):
    _translating_git(tmp_path, monkeypatch)
    wt = _add(repo, "racy")
    _age(repo, wt, 8 * DAY)
    real_git = worktree_reaper_remove.git

    def _racing_git(cwd: Path, args: list[str], **kwargs):
        result = real_git(cwd, args, **kwargs)
        into_quarantine = worktree_reaper_remove.QUARANTINE_DIR in args[-1]
        if args[:2] == ["worktree", "move"] and result.returncode == 0 and into_quarantine:
            (Path(args[-1]) / "late.txt").write_text("written mid-removal")
        return result

    monkeypatch.setattr(worktree_reaper_remove, "git", _racing_git)

    report = _run(repo)

    assert _reasons(report) == {"racy": "raced_dirty"}
    assert (wt / "late.txt").exists() and _quarantine(repo) == []


def test_reaper_git_runs_in_the_c_locale(repo, monkeypatch):
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    seen: dict[str, str] = {}
    real_run = subprocess.run

    def _spy(*args, **kwargs):
        seen.update(kwargs.get("env") or {})
        return real_run(*args, **kwargs)

    monkeypatch.setattr(worktree_reaper_probe.subprocess, "run", _spy)
    worktree_reaper_probe.git(repo, ["status", "--porcelain"])
    assert seen["LC_ALL"] == "C" and seen["LANG"] == "C"
    assert seen["PATH"] == os.environ["PATH"]


def _between_checks(monkeypatch, action) -> None:
    """Run ``action`` once, after the first checks and before the recheck."""
    fired: list[bool] = []

    def _skip_reason(self, worktree: Path) -> str | None:
        if not fired:
            fired.append(True)
            action(worktree)
        return None

    monkeypatch.setattr(FakeProbe, "skip_reason", _skip_reason)


def test_turn_started_after_the_first_check_stops_removal(repo, monkeypatch):
    wt = _add(repo, f"{UUID}-task")
    _age(repo, wt, 8 * DAY)
    reads: list[set[str]] = [set(), {UUID}]  # the turn starts after the first read

    def _active_threads() -> set[str]:
        return reads.pop(0) if len(reads) > 1 else reads[0]

    monkeypatch.setattr(worktree_reaper_checks, "_load_active_threads", _active_threads)

    report = _run(repo)

    assert _reasons(report) == {f"{UUID}-task": "thread_active"} and wt.is_dir()


def test_activity_after_the_first_check_stops_removal(repo, monkeypatch):
    wt = _add(repo, "touched")
    _age(repo, wt, 8 * DAY)
    _between_checks(monkeypatch, lambda path: os.utime(_admin(path) / "HEAD"))

    report = _run(repo)

    assert _reasons(report) == {"touched": "recent"} and wt.is_dir()


def test_commit_after_the_pr_mapping_stops_removal(repo, monkeypatch):
    mine = _add(repo, "mine", "feature")
    _age(repo, mine, 2 * 3600)
    pr_sha = _git(mine, "rev-parse", "HEAD")
    monkeypatch.setattr(worktree_reaper, "ProcessProbe", FakeProbe)

    def _commit(path: Path) -> None:
        # A clean new commit; the idle clock is aged again so only the
        # ancestry recheck can catch it.
        _git(path, "commit", "-q", "--allow-empty", "-m", "after merge")
        _age(repo, path, 2 * 3600)

    _between_checks(monkeypatch, _commit)

    report = worktree_reaper_pr.reap_merged_pr(
        _merged(repo, "feature", pr_sha), policies=[_policy(repo)],
    )

    assert _reasons(report) == {"mine": "ahead_of_pr"} and mine.is_dir()


@pytest.mark.asyncio
async def test_nightly_pass_failure_still_emits_the_metric(run_db, monkeypatch, isolated):
    from core import thread_lifecycle
    from jobs import nightly

    monkeypatch.setattr(thread_lifecycle, "run_lifecycle_retention", lambda: None)

    def _policies_unreadable():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(worktree_reaper, "list_registered_repo_policies", _policies_unreadable)

    await nightly._run_job_safely()

    assert len(isolated) == 1
    event = isolated[0]
    assert event["event"] == "worktree_reaper.run_completed"
    assert event["trigger"] == "idle"
    assert event["error"] == "pass_error: OperationalError: database is locked"
    with sqlite3.connect(run_db) as db:
        status = db.execute("SELECT status FROM fact_extraction_runs").fetchone()[0]
    assert status == "ok"


def test_salvage_copies_edits_and_non_cache_files(repo, monkeypatch, isolated):
    monkeypatch.setattr(worktree_reaper_checks, "open_pr_branches", lambda _: set())
    _exclude(repo, ".qa/\n.pip-cache/\n")
    wt = _add(repo, "salvage")
    (wt / "README").write_text("edited\n")
    (wt / "scratch").mkdir()
    (wt / "scratch" / "notes.md").write_text("notes\n")
    (wt / "scratch" / "odd\nname").write_text("odd\n")
    (wt / ".qa").mkdir()
    (wt / ".qa" / "report.json").write_text("{}")
    (wt / "node_modules").mkdir()
    (wt / "node_modules" / "x.js").write_text("cache")
    (wt / ".pip-cache").mkdir()
    (wt / ".pip-cache" / "y").write_text("cache")
    (wt / "scratch" / "link").symlink_to("notes.md")
    _age(repo, wt, 8 * DAY)

    report = _run(repo)
    saved = next((repo.parent / "repo-salvage").glob("*/salvage"))
    assert report.removed == report.salvaged == 1 and not wt.exists()
    assert "+edited" in (saved / "changes.patch").read_text()
    assert (saved / "files/scratch/notes.md").read_text() == "notes\n"
    assert (saved / "files/scratch" / "odd\nname").read_text() == "odd\n"
    assert (saved / "files/.qa/report.json").read_text() == "{}"
    assert (saved / "files/scratch/link").is_symlink()
    assert not (saved / "files/node_modules").exists()
    assert not (saved / "files/.pip-cache").exists()
    assert _git(repo, "branch", "--list", "b-salvage") == "b-salvage"
    emit_report(report)
    assert isolated[-1]["salvaged"] == 1


@pytest.mark.parametrize("prs,reason", [({"b-blocked"}, "open_pr"), (None, "pr_state_unknown")])
def test_salvage_requires_known_no_open_pr(repo, monkeypatch, prs, reason):
    monkeypatch.setattr(worktree_reaper_checks, "open_pr_branches", lambda _: prs)
    wt = _add(repo, "blocked")
    (wt / "notes").write_text("keep")
    _age(repo, wt, 8 * DAY)
    assert _reasons(_run(repo)) == {"blocked": reason} and wt.is_dir()


@pytest.mark.parametrize("condition,reason", [
    ("locked", "locked"), ("git_lock", "git_lock"),
    ("process", "in_use"), ("turn", "thread_active"),
])
def test_salvage_respects_existing_exclusions(repo, monkeypatch, condition, reason):
    monkeypatch.setattr(worktree_reaper_checks, "open_pr_branches", lambda _: set())
    name = f"{UUID}-salvage" if condition == "turn" else "salvage"
    wt = _add(repo, name)
    (wt / "notes").write_text("keep")
    if condition == "locked":
        _git(repo, "worktree", "lock", str(wt))
    elif condition == "git_lock":
        (_admin(wt) / "index.lock").write_text("")
    elif condition == "turn":
        monkeypatch.setattr(worktree_reaper_checks, "_load_active_threads", lambda: {UUID})
    _age(repo, wt, 8 * DAY)
    probe = FakeProbe({name: "in_use"} if condition == "process" else {})
    assert _reasons(_run(repo, probe)) == {name: reason} and wt.is_dir()


def test_failed_copy_keeps_worktree_and_removes_partial_salvage(repo, monkeypatch):
    from infra import worktree_reaper_salvage

    monkeypatch.setattr(worktree_reaper_checks, "open_pr_branches", lambda _: set())
    wt = _add(repo, "failed")
    (wt / "notes").write_text("keep")
    _age(repo, wt, 8 * DAY)

    def fail(*args, **kwargs):
        raise OSError("copy failed")

    monkeypatch.setattr(worktree_reaper_salvage.shutil, "copy2", fail)
    report = _run(repo)
    assert _reasons(report) == {"failed": "salvage_failed"} and wt.is_dir()
    assert not list((repo.parent / "repo-salvage").glob("*/failed"))


@pytest.mark.parametrize("days,removed", [(3, False), (8, True)])
def test_merged_dirty_requires_long_idle_and_salvage(repo, monkeypatch, days, removed):
    monkeypatch.setattr(worktree_reaper_checks, "open_pr_branches", lambda _: set())
    wt = _add(repo, "merged")
    (wt / "README").write_text("edited\n")
    _age(repo, wt, days * DAY)
    report = reap(_policy(repo), mode="pr_terminal", probe=FakeProbe())
    assert bool(report.removed) is removed
    if removed:
        assert report.salvaged == 1 and not wt.exists()
    else:
        assert _reasons(report) == {"merged": "dirty"} and wt.is_dir()
