"""Removal steps for the worktree reaper: quarantine first, never half-deleted.

A worktree leaves ``.worktrees/`` by one same-filesystem rename into
``<repo>/.git/corsia-reaping/``; only then is it deleted, and without
``--force`` so Git itself re-verifies that it is clean. A partial delete can
therefore only exist inside the quarantine, which each run retries.
"""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import shutil
import stat
import time

from loguru import logger

from infra.worktree_reaper_probe import git, git_detail, list_worktrees
from infra.worktree_reaper_salvage import SalvageError

QUARANTINE_DIR = "corsia-reaping"
_DIRTY_MARKERS = ("contains modified or untracked files", "is dirty")


def quarantine_root(repo: Path) -> Path:
    """Return the quarantine directory inside the base checkout's ``.git``."""
    return repo / ".git" / QUARANTINE_DIR


def _quarantine_target(repo: Path, name: str) -> Path:
    root = quarantine_root(repo)
    root.mkdir(exist_ok=True)
    stamp = int(time.time())
    target = root / f"{name}-{stamp}"
    suffix = 1
    while os.path.lexists(target):
        target = root / f"{name}-{stamp}-{suffix}"
        suffix += 1
    return target


def _chmod_dir(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            path.chmod(mode | stat.S_IRWXU)
    except OSError:
        pass


def _grant_owner_access(root: Path) -> None:
    """Give the owner rwx on every directory under ``root``, never via symlinks.

    ``rmtree`` cannot unlink from a read-only directory. Walking top-down fixes
    each child directory's mode before the walk descends into it.
    """
    _chmod_dir(root)
    for current, dirs, _files in os.walk(root):
        for name in dirs:
            _chmod_dir(Path(current) / name)


def delete_quarantined(repo: Path, path: Path) -> bool:
    """Delete one quarantine entry; return False if anything remains."""
    root = quarantine_root(repo)
    if path.parent != root or path.is_symlink():
        raise ValueError(f"{path} is not a quarantine entry")
    _grant_owner_access(path)
    try:
        shutil.rmtree(path)
    except OSError:
        return False
    return not os.path.lexists(path)


def quarantine_entries(repo: Path) -> list[str]:
    """Return every entry still in the quarantine; unreadable counts as one."""
    root = quarantine_root(repo)
    if not os.path.lexists(root):
        return []
    if root.is_symlink() or not root.is_dir():
        return [f"<{QUARANTINE_DIR} is not a directory>"]
    try:
        return sorted(entry.name for entry in root.iterdir())
    except OSError as exc:
        return [f"<unreadable: {exc.__class__.__name__}>"]


def sweep_quarantine(repo: Path) -> int:
    """Delete quarantined entries Git no longer tracks; return how many.

    An entry whose admin directory is still registered is left for a person
    to inspect; ``quarantine_entries`` reports whatever remains.
    """
    root = quarantine_root(repo)
    if not root.is_dir() or root.is_symlink():
        return 0
    registered = {entry.path for entry in list_worktrees(repo)}
    removed = 0
    for entry in sorted(root.iterdir()):
        if entry.resolve(strict=False) not in registered and delete_quarantined(repo, entry):
            removed += 1
    return removed


def remove_worktree(
    repo: Path, worktree: Path, name: str, *, force: bool = False,
    salvage: Callable[[Path], object] | None = None,
) -> str:
    """Quarantine then remove one checked worktree.

    ``force`` deletes scratch in a clean merged worktree or a worktree whose
    uncommitted changes ``salvage`` has just copied out of the quarantine. A
    failed salvage moves the worktree back and re-raises its SalvageError.

    Returns ``removed``, ``move_failed`` (still in place, untouched),
    ``raced_dirty`` (moved back after Git refused a dirty tree) or
    ``quarantine_pending`` (left in quarantine for the next run).
    """
    target = _quarantine_target(repo, name)
    moved = git(repo, ["worktree", "move", str(worktree), str(target)])
    if moved.returncode != 0:
        return "move_failed"
    if salvage is not None:
        try:
            salvage(target)
        except SalvageError:
            restored = git(repo, ["worktree", "move", str(target), str(worktree)])
            if restored.returncode != 0:
                # Still registered, so the quarantine sweep keeps it.
                logger.warning(
                    "worktree_reaper: {} left in quarantine after a failed salvage; move back: {}",
                    name, git_detail(restored),
                )
            raise
    removed = git(repo, ["worktree", "remove", *(["--force"] if force else []), str(target)])
    if removed.returncode == 0:
        return "removed"
    detail = git_detail(removed)
    restore = "not attempted"
    # Match the full message: git puts the worktree path first, so a long path
    # pushes the marker past git_detail's log-length cap.
    if any(marker in (removed.stderr or removed.stdout) for marker in _DIRTY_MARKERS):
        restored = git(repo, ["worktree", "move", str(target), str(worktree)])
        if restored.returncode == 0:
            return "raced_dirty"
        restore = git_detail(restored)
    logger.warning(
        "worktree_reaper: {} left in quarantine; remove: {}; move back: {}", name, detail, restore,
    )
    return "quarantine_pending"


def remove_orphan(repo: Path, orphan: Path) -> str:
    """Quarantine then delete an unregistered directory under ``.worktrees/``.

    Returns ``orphan_removed`` or ``quarantine_pending``; a failed rename
    leaves the directory where it was and returns ``move_failed``.
    """
    target = _quarantine_target(repo, orphan.name)
    try:
        os.rename(orphan, target)
    except OSError:
        return "move_failed"
    return "orphan_removed" if delete_quarantined(repo, target) else "quarantine_pending"
