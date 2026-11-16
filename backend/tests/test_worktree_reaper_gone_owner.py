"""Worktree reaper: a gone worker's worktree, and what a removal left behind.

A purged thread cannot come back to its worktree. Its tree goes once idle for
a day, uncommitted and unpublished work included, without a salvage copy. A
temporary worker archived for any reason can come back, so its tree goes after
a day too, but with its work copied out first. A gone owner's tree with
nothing uncommitted takes the ordinary clean removal. Every other owner,
including a name no thread matches, keeps the ordinary checks.
"""

from __future__ import annotations

import shutil
import sqlite3

import pytest

from infra import worktree_reaper, worktree_reaper_checks
from infra.worktree_reaper_checks import RemovalVerdict
from tests.test_worktree_reaper import (  # noqa: F401  (fixtures)
    DAY,
    FakeProbe,
    _add,
    _admin,
    _age,
    _age_all,
    _age_tree,
    _git,
    _reasons,
    _run,
    isolated,
    repo,
)

# The real reader, taken before the autouse fixture stubs it.
_REAL_LIFECYCLES = worktree_reaper_checks._load_thread_lifecycles

GONE = "feedc0de-1111-2222-3333-444455556666"
LIVE = "0123abcd-1111-2222-3333-444455556666"
SETTLED = "5e771ed0-1111-2222-3333-444455556666"
HOUR = 3600


@pytest.fixture
def owners(monkeypatch):
    table = {GONE: RemovalVerdict.DISCARD, LIVE: None, SETTLED: RemovalVerdict.SALVAGE}
    monkeypatch.setattr(worktree_reaper_checks, "_load_thread_lifecycles", lambda: dict(table))
    return table


def _unfinished(repo, name):
    """A worktree with an unpublished commit and an uncommitted edit."""
    wt = _add(repo, name)
    (wt / "README").write_text("committed, never pushed\n")
    _git(wt, "commit", "-q", "-am", "local only")
    (wt / "README").write_text("never committed\n")
    (wt / "scratch.txt").write_text("untracked\n")
    return wt


def test_a_gone_workers_unfinished_worktree_is_removed_without_a_copy(repo, owners):
    wt = _unfinished(repo, f"{GONE}-task")
    _age_all(repo, wt, 2 * DAY)

    report = _run(repo)

    assert report.removed == 1 and not wt.exists()
    assert report.salvaged == [] and report.discarded == [f"{GONE}-task"]
    assert _git(repo, "rev-parse", "--verify", f"b-{GONE}-task")  # the branch stays


def test_a_settled_workers_unfinished_worktree_is_removed_after_its_work_is_copied_out(
    repo, owners,
):
    wt = _unfinished(repo, f"{SETTLED}-task")
    _age_all(repo, wt, 2 * DAY)

    report = _run(repo)

    assert report.removed == 1 and not wt.exists()
    assert [item.name for item in report.salvaged] == [f"{SETTLED}-task"]
    assert report.discarded == []


@pytest.mark.parametrize("owner", [SETTLED, GONE])
def test_a_gone_owners_clean_worktree_takes_the_ordinary_clean_removal(
    repo, owners, monkeypatch, owner,
):
    wt = _add(repo, f"{owner}-task")
    _age_all(repo, wt, 2 * DAY)
    rechecked = []
    real_status_at = worktree_reaper_checks.status_at

    def _status_at(ctx, candidate, path, merged):
        rechecked.append(path)
        return real_status_at(ctx, candidate, path, merged)

    monkeypatch.setattr(worktree_reaper, "status_at", _status_at)

    report = _run(repo)

    assert report.removed == 1 and not wt.exists()
    assert report.salvaged == [] and report.discarded == []
    assert [path.parent.name for path in rechecked] == ["corsia-reaping"]  # re-checked in quarantine
    assert not (repo.parent / f"{repo.name}-salvage").exists()
    assert _git(repo, "for-each-ref", "refs/corsia-salvage/").strip() == ""


def test_a_gone_workers_worktree_with_a_file_edited_within_the_day_is_kept(repo, owners):
    # Git's files and the tree root are old; one file deep in the tree is not.
    wt = _unfinished(repo, f"{GONE}-task")
    _age_all(repo, wt, 2 * DAY)
    (wt / "ro" / "kept").write_text("still being finished\n")
    _age(repo, wt, 2 * DAY)

    report = _run(repo)

    assert report.removed == 0 and wt.is_dir()
    assert _reasons(report) == {f"{GONE}-task": "recent"}


def test_a_name_no_thread_matches_keeps_the_ordinary_checks(repo, owners):
    # A tree made by hand, or a database restored from before its owner existed.
    wt = _unfinished(repo, "deadbeef-task")
    _age_all(repo, wt, 2 * DAY)

    report = _run(repo)

    assert report.removed == 0 and wt.is_dir()
    assert _reasons(report) == {"deadbeef-task": "recent"}


def test_a_live_owners_unfinished_worktree_keeps_the_ordinary_checks(repo, owners):
    wt = _unfinished(repo, f"{LIVE}-task")
    _age_all(repo, wt, 2 * DAY)

    report = _run(repo)

    assert report.removed == 0 and wt.is_dir()
    assert _reasons(report) == {f"{LIVE}-task": "recent"}


def test_a_short_name_any_live_thread_could_mean_is_kept(repo, owners):
    owners["feedc0de-9999-2222-3333-444455556666"] = None
    wt = _unfinished(repo, "feedc0de-task")
    _age_all(repo, wt, 2 * DAY)

    report = _run(repo)

    assert report.removed == 0 and wt.is_dir()


def test_a_gone_workers_worktree_touched_within_the_day_is_kept(repo, owners):
    wt = _unfinished(repo, f"{GONE}-task")
    _age_all(repo, wt, 2 * HOUR)

    report = _run(repo)

    assert report.removed == 0 and wt.is_dir()


def test_a_live_process_still_keeps_a_gone_workers_worktree(repo, owners):
    wt = _unfinished(repo, f"{GONE}-task")
    _age_all(repo, wt, 2 * DAY)

    report = _run(repo, FakeProbe({f"{GONE}-task": "in_use"}))

    assert _reasons(report) == {f"{GONE}-task": "in_use"} and wt.is_dir()


def test_an_unreadable_thread_table_keeps_the_ordinary_checks(repo, monkeypatch):
    def _broken():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(worktree_reaper_checks, "_load_thread_lifecycles", _broken)
    wt = _unfinished(repo, f"{GONE}-task")
    _age_all(repo, wt, 2 * DAY)

    report = _run(repo)

    assert report.removed == 0 and wt.is_dir()


def test_a_tree_git_has_forgotten_is_removed_after_a_day(repo, owners):
    wt = _add(repo, "left-behind")
    shutil.rmtree(_admin(wt))
    for name in ("README", "ro"):
        target = wt / name
        shutil.rmtree(target) if target.is_dir() else target.unlink()
    (wt / "CLAUDE.md").write_text("x\n")
    _age_tree(wt, 2 * DAY)

    report = _run(repo)

    assert report.orphan_removed == 1 and not wt.exists()


def test_a_live_owners_full_tree_git_has_forgotten_is_kept(repo, owners):
    # `git worktree prune` can drop the record of a tree that is still in use.
    wt = _add(repo, f"{LIVE}-task")
    shutil.rmtree(_admin(wt))
    _age_tree(wt, 2 * DAY)

    report = _run(repo)

    assert report.orphan_removed == 0 and wt.is_dir()


def test_a_settled_owners_full_tree_git_has_forgotten_is_kept(repo, owners):
    # Orphans are never salvaged, and an archived worker can come back.
    wt = _add(repo, f"{SETTLED}-task")
    shutil.rmtree(_admin(wt))
    _age_tree(wt, 2 * DAY)

    report = _run(repo)

    assert report.orphan_removed == 0 and wt.is_dir()


def test_a_gone_owners_full_tree_git_has_forgotten_is_removed_after_a_day(repo, owners):
    wt = _add(repo, f"{GONE}-task")
    shutil.rmtree(_admin(wt))
    _age_tree(wt, 2 * DAY)

    report = _run(repo)

    assert report.orphan_removed == 1 and not wt.exists()


def test_a_forgotten_tree_touched_within_the_day_is_kept(repo, owners):
    wt = _add(repo, "left-behind")
    shutil.rmtree(_admin(wt))
    _age_tree(wt, 2 * HOUR)

    report = _run(repo)

    assert report.orphan_removed == 0 and wt.is_dir()


def test_thread_rows_are_classified_by_lifecycle(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE threads (id TEXT, spawned_by TEXT, agent_enabled INT, lifecycle_state TEXT,"
        " archived_reason TEXT)")
    connection.executemany("INSERT INTO threads VALUES (?,?,?,?,?)", [
        ("settled-worker", "parent", 0, "archived", "assignment_settled"),
        ("user-archived-worker", "parent", 0, "archived", "user"),
        ("dormant-worker", "parent", 0, "archived", "dormancy"),
        ("running-worker", "parent", 0, "active", None),
        ("archived-agent", "parent", 1, "archived", "user"),
        ("archived-root", None, 0, "archived", "user"),
        ("purged-root", None, 0, "purged", None),
        ("legacy-row", None, None, None, None),
    ])

    class _Unclosable:
        def __getattr__(self, name):
            return getattr(connection, name)

        def close(self):
            return None

    import infra.database

    monkeypatch.setattr(infra.database, "get_db", lambda: _Unclosable())

    assert _REAL_LIFECYCLES() == {
        "settled-worker": RemovalVerdict.SALVAGE, "user-archived-worker": RemovalVerdict.SALVAGE,
        "dormant-worker": RemovalVerdict.SALVAGE, "running-worker": None, "archived-agent": None,
        "archived-root": None, "purged-root": RemovalVerdict.DISCARD, "legacy-row": None,
    }
