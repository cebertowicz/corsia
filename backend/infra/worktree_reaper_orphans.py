"""Checks for unregistered entries under ``.worktrees/``: what Git no longer verifies."""

from __future__ import annotations

import os
from pathlib import Path

from infra.worktree_reaper_checks import GONE_OWNER_IDLE_SECONDS, CheckContext
from infra.worktree_reaper_probe import ReaperGitError, list_worktrees
from infra.worktree_reaper_scan import Orphan, newest_tree_mtime, owner_thread


def _forgotten_by_git(repo: Path, path: Path) -> bool:
    """Whether ``path/.git`` is a file naming a missing record under ``repo/.git/worktrees/``."""
    link = path / ".git"
    if link.is_symlink() or not link.is_file():
        return False
    try:
        text = link.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if not text.startswith("gitdir: "):
        return False
    target = Path(text[len("gitdir: "):].strip())
    records = (repo / ".git" / "worktrees").resolve(strict=False)
    if not target.is_absolute() or target.resolve(strict=False).parent != records:
        return False
    return not os.path.lexists(target)


def _is_shell(path: Path) -> bool:
    """Whether ``path`` holds only files, as a removal leaves behind; a working tree has directories."""
    try:
        with os.scandir(path) as entries:
            return not any(entry.is_dir(follow_symlinks=False) for entry in entries)
    except OSError:
        return False


def orphan_skip_reason(
    ctx: CheckContext, orphan: Orphan, threshold_seconds: float,
) -> tuple[str | None, float | None]:
    """Return why an unregistered ``.worktrees/`` entry must be kept, and its age.

    An orphan is deleted only when it has no ``.git`` entry, Git does not
    list it, nothing anywhere inside changed for the threshold, and no live
    process uses it. A ``.git`` file that points at this repository's own
    worktree records, at one that no longer exists, does not keep a tree Git
    has already forgotten when either it is a shell a removal left behind (no
    subdirectory) or its owner is gone; it then goes once idle for
    ``GONE_OWNER_IDLE_SECONDS``. A full tree whose record was pruned while its
    owner lives keeps its ``.git`` entry's protection. Every check reads live
    state, so it doubles as the recheck.
    """
    path = orphan.path
    if path.is_symlink():
        return "symlink", None
    forgotten = _forgotten_by_git(ctx.repo, path) and (
        _is_shell(path)
        or ctx.owner_verdict(owner_thread(orphan.name), fresh=True) is not None
    )
    if not forgotten and os.path.lexists(path / ".git"):
        return "git_entry_present", None
    if forgotten:
        threshold_seconds = min(threshold_seconds, GONE_OWNER_IDLE_SECONDS)
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
