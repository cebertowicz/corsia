"""Removal steps for the worktree reaper: quarantine first, never half-deleted.

A worktree leaves ``.worktrees/`` by one same-filesystem rename into
``<repo>/.git/corsia-reaping/``; only then is it deleted. The removal path is the
one place that decides an entry may be deleted: once its work is copied out, the
check that cleared it passes again at the quarantine path, or its owner is purged
(nothing is owed to an owner that cannot return), it writes a
copied-out record beside the entry, and only then deletes. ``sweep_quarantine``
finishes an entry that has a record (a removal cut off or refused) and keeps
every entry without one for a person.
"""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import shutil
import stat
import time
from typing import TYPE_CHECKING

from loguru import logger

from infra.repo_flock import RepoBusyError, repo_lock
from infra.worktree_reaper_probe import (
    WORKTREE_REMOVE_TIMEOUT_SECONDS,
    ReaperGitError,
    admin_dir,
    common_git_env,
    git,
    git_detail,
    list_worktrees,
)
from infra.worktree_reaper_salvage import SalvageError

if TYPE_CHECKING:
    import subprocess

    from infra.worktree_reaper import RunBudget

QUARANTINE_DIR = "corsia-reaping"
# Suffix of the record beside a quarantine entry that says nothing in it is owed to
# anyone. Only the removal path writes it, once the entry is in the quarantine and
# its work is copied out, its check passed there or its owner is purged. It is the only thing that lets
# any pass delete an entry: losing it can strand an entry, never delete one.
COPIED_OUT_SUFFIX = ".copied-out"


def quarantine_root(repo: Path) -> Path:
    """Return the quarantine directory inside the base checkout's ``.git``."""
    return repo / ".git" / QUARANTINE_DIR


def _copied_out_record(entry: Path) -> Path:
    return entry.with_name(entry.name + COPIED_OUT_SUFFIX)


def _quarantine_target(repo: Path, name: str) -> Path:
    """Pick an unused entry name; one whose record is still there is not unused."""
    root = quarantine_root(repo)
    root.mkdir(exist_ok=True)
    stamp = int(time.time())
    target = root / f"{name}-{stamp}"
    suffix = 1
    while os.path.lexists(target) or os.path.lexists(_copied_out_record(target)):
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
    """Return every entry still in the quarantine; unreadable counts as one.

    A copied-out record is not an entry: it names the entry it belongs to.
    """
    root = quarantine_root(repo)
    if not os.path.lexists(root):
        return []
    if root.is_symlink() or not root.is_dir():
        return [f"<{QUARANTINE_DIR} is not a directory>"]
    try:
        return sorted(
            entry.name for entry in root.iterdir()
            if not entry.name.endswith(COPIED_OUT_SUFFIX)
        )
    except OSError as exc:
        return [f"<unreadable: {exc.__class__.__name__}>"]


def mark_copied_out(entry: Path) -> None:
    """Record that nothing in ``entry`` is owed to anyone any more.

    Only the removal path calls it, once the entry exists in the quarantine and
    its work is copied out, its check passed there or its owner is purged.
    """
    _copied_out_record(entry).write_text(f"{entry.name}\n", encoding="utf-8")


def is_copied_out(entry: Path) -> bool:
    """Return whether the removal path recorded that this entry owes nobody anything."""
    return _copied_out_record(entry).is_file()


def _drop_record(entry: Path) -> None:
    """Drop an entry's record; call only once the entry itself is gone."""
    record = _copied_out_record(entry)
    try:
        record.unlink(missing_ok=True)
    except OSError as exc:
        # Harmless: the record stays, and the next sweep drops it once its entry is gone.
        logger.warning("worktree_reaper: could not drop {}: {}", record.name, exc)


def _registered(repo: Path, entry: Path) -> bool:
    """Return whether Git still registers ``entry`` at its path or by its admin directory."""
    admin = admin_dir(entry)
    if admin is not None and admin.is_dir():
        return True
    return entry.resolve(strict=False) in {item.path for item in list_worktrees(repo)}


def _remove(repo: Path, entry: Path) -> subprocess.CompletedProcess[str]:
    """Run Git's forced removal; a real tree takes minutes, so it has its own timeout."""
    return git(
        repo, ["worktree", "remove", "--force", str(entry)],
        timeout=WORKTREE_REMOVE_TIMEOUT_SECONDS, git_env=common_git_env(repo),
    )


def _finish(repo: Path, entry: Path) -> bool:
    """Delete an entry that has a record; on failure the record stays for the next sweep.

    Git's forced removal deletes the tree and then its registration. A removal cut
    off mid-delete can leave a tree Git refuses (its ``.git`` file already gone).
    The record authorizes deletion, so the directory is deleted here and Git is
    asked once more, which then drops only this entry's registration. It counts
    only once Git registers the entry nowhere.
    """
    removed = _remove(repo, entry)
    if removed.returncode != 0:
        if os.path.lexists(entry) and not delete_quarantined(repo, entry):
            logger.warning("worktree_reaper: entry {} kept; delete failed", entry.name)
            return False
        if _registered(repo, entry):
            again = _remove(repo, entry)
            if _registered(repo, entry):
                logger.warning(
                    "worktree_reaper: entry {} kept; git refused: {}", entry.name, git_detail(again),
                )
                return False
    _drop_record(entry)
    return True


def sweep_quarantine(repo: Path, budget: RunBudget) -> int:
    """Finish the entries a removal recorded and left; return how many went.

    An entry with a copied-out record is finished (``_finish``). An entry without
    one is kept whatever state it is in, and every run report lists it until a
    person acts. A record whose entry is gone is deleted, unless Git still
    registers that entry: then it is finished. Each finish changes Git's worktree
    registry, so it runs under the repository lock, and the sweep stops when
    another Git mutator holds it. ``budget`` is checked before each entry, and
    each finished entry counts as one removal.
    """
    root = quarantine_root(repo)
    if not root.is_dir() or root.is_symlink():
        return 0
    finished = 0
    for entry in sorted(root.iterdir()):
        if entry.name.endswith(COPIED_OUT_SUFFIX):
            record, entry = entry, entry.with_name(entry.name.removesuffix(COPIED_OUT_SUFFIX))
            if os.path.lexists(entry):
                continue
            try:
                gone = not _registered(repo, entry)
            except (OSError, ReaperGitError):
                gone = False  # Unknown: keep the record for the next sweep.
            if gone:
                _drop_record(entry)
                continue
        elif not is_copied_out(entry):
            continue
        if budget.exhausted():
            break
        try:
            with repo_lock(repo):
                done = _finish(repo, entry)
        except RepoBusyError:
            break  # The next pass finishes the rest.
        except (OSError, ReaperGitError) as exc:
            logger.warning("worktree_reaper: could not finish entry {}: {}", entry.name, exc)
            done = False
        if done:
            finished += 1
            budget.removals_left -= 1
    return finished


def _move_back(repo: Path, target: Path, worktree: Path, name: str) -> bool:
    restored = git(repo, ["worktree", "move", str(target), str(worktree)])
    if restored.returncode != 0:
        # It has no record, so every sweep keeps it and every run report lists it.
        logger.warning(
            "worktree_reaper: {} kept in quarantine; move back: {}", name, git_detail(restored),
        )
    return restored.returncode == 0


def remove_worktree(
    repo: Path, worktree: Path, name: str, clear: Callable[[Path], str | None],
) -> str:
    """Quarantine, clear and remove one checked worktree.

    ``clear`` gets the quarantined path and copies its work out, runs the check
    that cleared it again there, or (owner purged) does neither. It returns None when nothing in the tree is
    owed to anyone, or the reason something is. That answer is the one decision to
    delete: only after it is the record written, and only then the tree removed.
    A reason moves the tree back; so does a failed clear, which re-raises.

    Returns ``removed``, ``move_failed`` (still in place, untouched),
    ``raced_dirty`` (moved back: work appeared after the checks) or
    ``quarantine_pending`` (left in quarantine: with a record the next sweep
    finishes it, without one it waits for a person).
    """
    target = _quarantine_target(repo, name)
    moved = git(repo, ["worktree", "move", str(worktree), str(target)])
    if moved.returncode != 0:
        return "move_failed"
    try:
        reason = clear(target)
    except (SalvageError, ReaperGitError, OSError):
        _move_back(repo, target, worktree, name)
        raise
    if reason is not None:
        return "raced_dirty" if _move_back(repo, target, worktree, name) else "quarantine_pending"
    try:
        mark_copied_out(target)
    except OSError as exc:
        logger.warning("worktree_reaper: {} kept in quarantine; no record: {}", name, exc)
        return "quarantine_pending"
    if _finish(repo, target):
        return "removed"
    logger.warning("worktree_reaper: {} left in quarantine for the next sweep", name)
    return "quarantine_pending"


def remove_orphan(repo: Path, orphan: Path) -> str:
    """Quarantine, record, then delete an unregistered directory under ``.worktrees/``.

    The orphan checks the caller just ran under the repository lock are the
    decision. Returns ``orphan_removed`` or ``quarantine_pending``; a failed rename
    leaves the directory where it was and returns ``move_failed``.
    """
    target = _quarantine_target(repo, orphan.name)
    try:
        os.rename(orphan, target)
    except OSError:
        return "move_failed"
    try:
        mark_copied_out(target)
    except OSError as exc:
        logger.warning("worktree_reaper: orphan {} kept in quarantine; no record: {}", orphan.name, exc)
        return "quarantine_pending"
    if not delete_quarantined(repo, target):
        return "quarantine_pending"
    _drop_record(target)
    return "orphan_removed"
