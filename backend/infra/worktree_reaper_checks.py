"""Safety checks for the worktree reaper, cheapest first.

The first failed check ends a worktree's processing and names the skip reason.
Every check fails closed: a probe error is a skip with its own reason, never
a pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import os
from pathlib import Path

from loguru import logger

from infra.worktree_reaper_probe import (
    ProcessProbe,
    ReaperGitError,
    WorktreeEntry,
    git_lock_files,
    has_unpublished_commits,
    is_ancestor,
    list_worktrees,
    open_pr_branches,
    status_skip_reason,
)
from infra.worktree_reaper_scan import Candidate, Orphan, idle_since, newest_tree_mtime


def _load_active_threads() -> set[str]:
    from core.turn_core import active_turn_thread_ids

    return active_turn_thread_ids()


@dataclass
class CheckContext:
    """Per-run state shared by every check in one repository pass."""

    repo: Path
    probe: ProcessProbe
    now: float
    _active_threads: set[str] | None = field(default=None, init=False)
    _threads_loaded: bool = field(default=False, init=False)
    _open_prs: set[str] | None = field(default=None, init=False)
    _prs_loaded: bool = field(default=False, init=False)

    def open_pr_branches(self, *, fresh: bool = False) -> set[str] | None:
        """Open-PR head branches, read once per pass; None when unknown.

        ``fresh`` re-reads them, as the pre-removal recheck must.
        """
        if fresh or not self._prs_loaded:
            self._prs_loaded = True
            self._open_prs = open_pr_branches(self.repo)
        return self._open_prs

    def is_recent(
        self, entry: WorktreeEntry | None, idle_since_value: float,
        threshold_seconds: float, short_threshold_seconds: float | None,
    ) -> bool:
        """Return whether the idle clock still protects a worktree.

        The short clock replaces the long one only for a worktree whose HEAD is
        contained in a remote-tracking ref and whose branch has no open PR; an
        unknown PR set keeps the long clock.
        """
        idle = self.now - idle_since_value
        if idle >= threshold_seconds:
            return False
        if short_threshold_seconds is None or idle < short_threshold_seconds:
            return True
        if entry is None or entry.head is None or has_unpublished_commits(self.repo, entry.head):
            return True
        open_prs = self.open_pr_branches()
        if open_prs is None:
            return True
        branch = (entry.branch or "").removeprefix("refs/heads/")
        return branch in open_prs

    def thread_skip_reason(self, owner: str | None, *, fresh: bool = False) -> str | None:
        """Return ``thread_active`` when the owning thread has a running turn.

        The first call reads the active turns once per pass; ``fresh`` re-reads
        them, as the pre-removal recheck must.
        """
        if owner is None:
            return None
        if fresh or not self._threads_loaded:
            self._threads_loaded = True
            try:
                self._active_threads = _load_active_threads()
            except Exception as exc:
                logger.warning("worktree_reaper: active turn read failed: {}", exc)
                self._active_threads = None
        if self._active_threads is None:
            return "thread_state_unknown"
        if any(thread == owner or thread.startswith(owner) for thread in self._active_threads):
            return "thread_active"
        return None


def _status_reason(path: Path, merged: bool) -> str | None:
    try:
        return status_skip_reason(path, tracked_only=merged)
    except ReaperGitError:
        return "status_error"


def _entry_reason(entry: WorktreeEntry | None) -> str | None:
    if entry is None:
        return "unregistered"
    if entry.locked:
        return "locked"
    if entry.prunable:
        return "orphan"
    return None


class RemovalVerdict(Enum):
    SALVAGE = "salvage"


def _status_verdict(
    ctx: CheckContext, entry: WorktreeEntry | None, path: Path, status: str | None,
    idle: float, threshold: float,
) -> str | RemovalVerdict | None:
    if status is None or status == "status_error":
        return status
    if ctx.now - idle < threshold:
        return status
    # Git's admin files move only on Git commands; an editor writes the tree.
    # Uncommitted work leaves only when nothing anywhere in it changed either.
    newest = newest_tree_mtime(path)
    if newest is None or ctx.now - newest < threshold:
        return status
    prs = ctx.open_pr_branches()
    if prs is None:
        return "pr_state_unknown"
    if entry and (entry.branch or "").removeprefix("refs/heads/") in prs:
        return "open_pr"
    return RemovalVerdict.SALVAGE


def candidate_verdict(
    ctx: CheckContext, candidate: Candidate, threshold_seconds: float,
    *, merged: bool = False, short_threshold_seconds: float | None = None,
) -> str | RemovalVerdict | None:
    """Return a skip reason, salvage verdict, or clean removal verdict.

    Merged clean worktrees retain their immediate removal path. Tracked edits
    require the long idle clock and salvage even for merged worktrees.
    """
    reason = _entry_reason(candidate.entry)
    if reason:
        return reason
    if git_lock_files(candidate.admin):
        return "git_lock"
    if not merged and ctx.is_recent(
        candidate.entry, candidate.idle_since, threshold_seconds, short_threshold_seconds,
    ):
        return "recent"
    verdict = _status_verdict(
        ctx, candidate.entry, candidate.path, _status_reason(candidate.path, merged),
        candidate.idle_since, threshold_seconds,
    )
    if isinstance(verdict, str):
        return verdict
    return (ctx.probe.skip_reason(candidate.path)
            or (None if merged else ctx.thread_skip_reason(candidate.owner_thread))
            or verdict)


def recheck_before_removal(
    ctx: CheckContext,
    candidate: Candidate,
    threshold_seconds: float,
    expected_head: str | None = None,
    *, merged: bool = False, short_threshold_seconds: float | None = None,
) -> str | RemovalVerdict | None:
    """Repeat every check against live state just before acting.

    A pass can run for minutes after the first check, so nothing cached is
    trusted here: registration, locks, the idle clock, the worktree status,
    live processes and running turns are all read again, under the same
    ``merged`` rules as the first check. The open-PR set is the exception:
    the caller re-reads it just before taking the repo lock, so a slow ``gh``
    call never holds that lock. ``expected_head`` (the merged PR's
    head, or the head recorded at merge time) must still contain the
    worktree's current HEAD.
    """
    try:
        entry = next(
            (item for item in list_worktrees(ctx.repo) if item.path == candidate.path),
            None,
        )
    except ReaperGitError:
        return "status_error"
    reason = _entry_reason(entry)
    if reason:
        return reason
    if git_lock_files(candidate.admin):
        return "git_lock"
    idle = idle_since(candidate.path, candidate.admin)
    if not merged and ctx.is_recent(entry, idle, threshold_seconds, short_threshold_seconds):
        return "recent"
    if expected_head is not None:
        head = entry.head if entry else None
        try:
            if head is None or not is_ancestor(ctx.repo, head, expected_head):
                return "ahead_of_pr"
        except ReaperGitError:
            return "status_error"
    verdict = _status_verdict(
        ctx, entry, candidate.path, _status_reason(candidate.path, merged), idle,
        threshold_seconds,
    )
    if isinstance(verdict, str):
        return verdict
    ctx.probe.refresh()
    return (ctx.probe.skip_reason(candidate.path)
            or (None if merged else ctx.thread_skip_reason(candidate.owner_thread, fresh=True))
            or verdict)


def orphan_skip_reason(
    ctx: CheckContext, orphan: Orphan, threshold_seconds: float,
) -> tuple[str | None, float | None]:
    """Return why an unregistered ``.worktrees/`` entry must be kept, and its age.

    An orphan is deleted only when it has no ``.git`` entry, Git does not
    list it, nothing anywhere inside changed for the threshold, and no live
    process uses it. Every check reads live state, so it doubles as the recheck.
    """
    path = orphan.path
    if path.is_symlink():
        return "symlink", None
    if os.path.lexists(path / ".git"):
        return "git_entry_present", None
    try:
        registered = {item.path for item in list_worktrees(ctx.repo)}
    except ReaperGitError:
        return "status_error", None
    if path.resolve(strict=False) in registered:
        return "registered", None
    newest = newest_tree_mtime(path)
    if newest is None:
        return "unreadable", None
    idle_days = round(max(ctx.now - newest, 0.0) / 86400, 2)
    if ctx.now - newest < threshold_seconds:
        return "recent", idle_days
    ctx.probe.refresh()
    return ctx.probe.skip_reason(path), idle_days
