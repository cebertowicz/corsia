"""Removal steps for the worktree reaper: quarantine first, never half-deleted.

A worktree leaves ``.worktrees/`` by one same-filesystem rename into
``<repo>/.git/corsia-reaping/``; only then is it deleted, and without
``--force`` so Git itself re-verifies that it is clean. A partial delete can
therefore only exist inside the quarantine, which each run finishes -- see
``sweep_quarantine``, which is also where a removal that ran out of time gets its
second chance. Work the reaper never copied out of an entry is recorded on the
entry itself, and a later pass never deletes it.
"""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import shutil
import stat
import time

from loguru import logger

from infra.worktree_reaper_probe import (
    WORKTREE_REMOVE_TIMEOUT_SECONDS,
    ReaperGitError,
    admin_dir,
    common_git_env,
    git,
    git_detail,
    list_worktrees,
    status_skip_reason,
    worktree_git_env,
)
from infra.worktree_reaper_salvage import SalvageError

QUARANTINE_DIR = "corsia-reaping"
# Suffix of the record beside a quarantine entry that still holds work the
# reaper never copied out of it. It is named after its entry, so listing the
# quarantine shows which entry the reaper would not delete and why, and it is
# written before the step that can be cut off rather than after it.
UNSAVED_WORK_SUFFIX = ".unsaved-work"
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
    """Return every entry still in the quarantine; unreadable counts as one.

    An unsaved-work record is not an entry: it names the entry it belongs to.
    """
    root = quarantine_root(repo)
    if not os.path.lexists(root):
        return []
    if root.is_symlink() or not root.is_dir():
        return [f"<{QUARANTINE_DIR} is not a directory>"]
    try:
        return sorted(
            entry.name for entry in root.iterdir()
            if not entry.name.endswith(UNSAVED_WORK_SUFFIX)
        )
    except OSError as exc:
        return [f"<unreadable: {exc.__class__.__name__}>"]


def _unsaved_work_record(entry: Path) -> Path:
    return entry.with_name(entry.name + UNSAVED_WORK_SUFFIX)


def mark_unsaved_work(entry: Path, reason: str) -> None:
    """Record that ``entry`` holds work the reaper never copied out of it.

    The entry carries its own protection, so a pass cut off at any point leaves
    the safe answer behind: a later sweep keeps the directory and reports it.
    """
    record = _unsaved_work_record(entry)
    record.write_text(f"{entry.name}: {reason}\n", encoding="utf-8")


def clear_unsaved_work(entry: Path) -> None:
    """Drop the record once the entry's work is safely copied out."""
    record = _unsaved_work_record(entry)
    try:
        record.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        # Harmless: once the entry itself is gone, the sweep deletes the record.
        logger.warning("worktree_reaper: could not clear {}: {}", record.name, exc)


def holds_unsaved_work(entry: Path) -> bool:
    """Return whether the reaper stopped before this entry's work was copied out."""
    return _unsaved_work_record(entry).exists()


def _stranded_move(entry: Path) -> bool:
    """Return whether Git still points this entry's admin directory elsewhere.

    That is the signature of a pass cut off during ``git worktree move``: the
    directory is here, the registry still names the path it left, and nothing has
    been copied out of it. The tree's own check then decides -- see
    ``_finish_stranded_move``.
    """
    admin = admin_dir(entry)
    if admin is None or not (admin / "gitdir").is_file():
        return False
    try:
        claimed = (admin / "gitdir").read_text(encoding="utf-8").strip()
    except OSError:
        return True
    return Path(claimed).resolve(strict=False) != (entry / ".git").resolve(strict=False)


def _finish_stranded_move(repo: Path, entry: Path) -> bool:
    """Reclaim a quarantine entry whose move was cut off before the registry update.

    Nothing was copied out of it, so the tree's own check decides, the same check
    that cleared it for removal in the first place: a tree with nothing unshipped
    in it is debris and goes, and one that still holds work waits for a person and
    is reported. Repairing Git's registry by hand would be the alternative; the
    reaper does not write it.
    """
    admin = admin_dir(entry)
    if admin is None:
        return False
    try:
        if status_skip_reason(entry, worktree_git_env(repo, admin, entry)) is not None:
            logger.warning(
                "worktree_reaper: entry {} still holds work; left for a person", entry.name
            )
            return False
        if os.path.lexists(entry) and not delete_quarantined(repo, entry):
            logger.warning("worktree_reaper: stranded entry {} could not be deleted", entry.name)
            return False
        git(repo, ["worktree", "prune"], git_env=common_git_env(repo))
        return True
    except (OSError, ReaperGitError) as exc:
        logger.warning("worktree_reaper: could not judge entry {}: {}", entry.name, exc)
        return False


def _finish_removal(repo: Path, entry: Path) -> bool:
    """Finish a removal an earlier pass started; False when something remains.

    ``--force`` is owed here because the whole set of checks ran before the entry
    was moved in, and whatever was still unshipped was copied out or marked.
    """
    try:
        removed = git(
            repo, ["worktree", "remove", "--force", str(entry)],
            timeout=WORKTREE_REMOVE_TIMEOUT_SECONDS, git_env=common_git_env(repo),
        )
        if removed.returncode == 0:
            return True
        detail = git_detail(removed)
        # Git drops the registration before it finishes deleting the tree, so a
        # refusal often leaves a directory Git no longer knows about, with no
        # second question left to ask it.
        if os.path.lexists(entry) and not delete_quarantined(repo, entry):
            logger.warning("worktree_reaper: entry {} still there: {}", entry.name, detail)
            return False
        git(repo, ["worktree", "prune"], git_env=common_git_env(repo))
        return True
    except (OSError, ReaperGitError) as exc:
        logger.warning("worktree_reaper: could not finish entry {}: {}", entry.name, exc)
        return False


def sweep_quarantine(repo: Path) -> int:
    """Finish what earlier passes left in the quarantine; return how many went.

    Every entry here passed the full set of checks in the pass that moved it in,
    so this deletes nothing that pass had not cleared. It sorts four cases apart:

    * marked as holding unsaved work: left for a person, and reported by
      ``quarantine_entries``;
    * still claimed by Git under the path it left, a move cut off before its
      registry update: the tree's own check decides, see ``_finish_stranded_move``;
    * registered at this path: its removal was cut off mid-delete, so this is the
      only place a later pass can finish it;
    * known to Git nowhere: its admin directory is gone, meaning the removal
      finished and only the directory was left.
    """
    root = quarantine_root(repo)
    if not root.is_dir() or root.is_symlink():
        return 0
    registered = {entry.path for entry in list_worktrees(repo)}
    removed = 0
    for entry in sorted(root.iterdir()):
        if entry.name.endswith(UNSAVED_WORK_SUFFIX):
            if not os.path.lexists(entry.with_name(entry.name[: -len(UNSAVED_WORK_SUFFIX)])):
                entry.unlink(missing_ok=True)  # Its entry is gone; this is only a name now.
            continue
        if holds_unsaved_work(entry):
            continue
        if _stranded_move(entry):
            finished = _finish_stranded_move(repo, entry)
        else:
            finished = (
                _finish_removal(repo, entry)
                if entry.resolve(strict=False) in registered
                else delete_quarantined(repo, entry)
            )
        removed += 1 if finished else 0
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
    if salvage is not None:
        # Everything between this move and the copy finishing can be cut off, and
        # an entry cut off in it holds work nobody has copied out yet. The entry
        # says so before it moves, so a later sweep cannot mistake it for debris.
        mark_unsaved_work(target, f"moved in from {worktree} before its work was copied out")
    moved = git(repo, ["worktree", "move", str(worktree), str(target)])
    if moved.returncode != 0:
        clear_unsaved_work(target)  # Nothing moved; there is no entry to protect.
        return "move_failed"
    if salvage is not None:
        try:
            salvage(target)
        except SalvageError:
            restored = git(repo, ["worktree", "move", str(target), str(worktree)])
            if restored.returncode == 0:
                clear_unsaved_work(target)  # Back with its owner, out of the sweep's way.
            else:
                # Still registered here, and the record written before the move
                # stays: its work was never copied out, so the sweep keeps it.
                logger.warning(
                    "worktree_reaper: {} left in quarantine after a failed salvage; move back: {}",
                    name, git_detail(restored),
                )
            raise
        clear_unsaved_work(target)  # The work is copied out and pinned now.
    # Without --force Git spawns its own status in the worktree; the child
    # inherits this pin but finds its GIT_DIR through the worktree. Deleting a
    # real tree takes minutes, so this call answers to the removal timeout.
    removed = git(
        repo, ["worktree", "remove", *(["--force"] if force else []), str(target)],
        timeout=WORKTREE_REMOVE_TIMEOUT_SECONDS, git_env=common_git_env(repo),
    )
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
        # A file appeared and the entry could not be handed back to its owner, so
        # it stays put with its work un-copied: the sweep must not finish it.
        mark_unsaved_work(target, "a file appeared during removal and it could not be moved back")
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
