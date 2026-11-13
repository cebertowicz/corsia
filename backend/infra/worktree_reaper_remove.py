"""Removal steps for the worktree reaper: quarantine first, never half-deleted.

A worktree leaves ``.worktrees/`` by one same-filesystem rename into
``<repo>/.git/corsia-reaping/``; only then is it deleted. A partial delete can
therefore only exist inside the quarantine, which each run finishes -- see
``sweep_quarantine``, which is also where a removal that ran out of time gets its
second chance. A later pass deletes an entry Git still registers only on positive
evidence that nothing in it is owed to anyone; without that evidence it keeps it.
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
# Suffix of the record beside a quarantine entry that says nothing in it is owed to
# anyone: its uncommitted work was copied out, or the checks before the move
# cleared a forced removal. It is written only once the entry exists and that is
# true, so it is positive evidence: losing it can strand an entry, never delete one.
COPIED_OUT_SUFFIX = ".copied-out"
_DIRTY_MARKERS = ("contains modified or untracked files", "is dirty")


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

    Call it only once the entry exists in the quarantine and its work is copied
    out, or the checks found none: the sweep force-removes an entry on it.
    """
    _copied_out_record(entry).write_text(f"{entry.name}\n", encoding="utf-8")


def is_copied_out(entry: Path) -> bool:
    """Return whether the reaper recorded that this entry owes nobody anything."""
    return _copied_out_record(entry).is_file()


def _drop_record(entry: Path) -> None:
    """Drop an entry's record; call only once the entry itself is gone."""
    record = _copied_out_record(entry)
    try:
        record.unlink(missing_ok=True)
    except OSError as exc:
        # Harmless: the sweep deletes a record whose entry is gone.
        logger.warning("worktree_reaper: could not drop {}: {}", record.name, exc)


def _keep_for_a_person(entry: Path, reason: str) -> bool:
    """Report an entry the reaper will not delete; always returns False.

    ``quarantine_entries`` lists it in every run report until a person acts.
    """
    logger.warning("worktree_reaper: entry {} kept for a person: {}", entry.name, reason)
    return False


def _claimed_path(admin: Path) -> Path | None:
    """Return the worktree path Git's registration in ``admin`` names, if readable."""
    try:
        raw = (admin / "gitdir").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    claimed = Path(raw)
    # With worktree.useRelativePaths the record is relative to the admin directory.
    return (claimed if claimed.is_absolute() else admin / claimed).resolve(strict=False).parent


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
    return _claimed_path(admin) != entry.resolve(strict=False)


def _finish_stranded_move(repo: Path, entry: Path) -> bool:
    """Reclaim a quarantine entry whose move was cut off before the registry update.

    Nothing was copied out of it, so the tree's own check decides, the same check
    that cleared it for removal in the first place: a tree with nothing unshipped
    in it is debris and goes, and one that still holds work waits for a person and
    is reported. Its registration is dropped by the path Git still has for it, one
    entry only: ``git worktree prune`` would also drop the registration of every
    other stranded entry kept here, and the next sweep would read that entry as
    known to Git nowhere and delete it.
    """
    admin = admin_dir(entry)
    if admin is None:
        return False
    try:
        if status_skip_reason(entry, worktree_git_env(repo, admin, entry)) is not None:
            return _keep_for_a_person(entry, "its move was cut off before any copy")
        claimed = _claimed_path(admin)
        if claimed is None or os.path.lexists(claimed):
            return _keep_for_a_person(entry, f"its registration names {claimed}, which is there")
        # The claimed path is gone, so Git drops only the registration (git 2.53.0).
        dropped = git(
            repo, ["worktree", "remove", "--force", str(claimed)], git_env=common_git_env(repo),
        )
        if dropped.returncode != 0:
            return _keep_for_a_person(entry, f"registration not dropped: {git_detail(dropped)}")
        if os.path.lexists(entry) and not delete_quarantined(repo, entry):
            logger.warning("worktree_reaper: stranded entry {} could not be deleted", entry.name)
            return False
        return True
    except (OSError, ReaperGitError) as exc:
        logger.warning("worktree_reaper: could not judge entry {}: {}", entry.name, exc)
        return False


def _not_a_worktree(entry: Path, output: str) -> bool:
    """Return whether Git refused because it no longer registers ``entry`` at all.

    git 2.53.0 says, for a path it has no registration for::

        fatal: '<path>' is not a working tree
    """
    return f"'{entry}' is not a working tree" in output


def _finish_removal(repo: Path, entry: Path) -> bool:
    """Finish a removal an earlier pass started, on positive evidence only.

    The entry is force-removed when its copied-out record says nothing in it is
    owed, or when its tree shows nothing but the deletions a cut-off removal makes
    and disposable output. Anything else keeps it: no record and an edit, an
    untracked file or an unreadable tree, or any Git refusal except that Git no
    longer registers the path, which makes it an entry known to Git nowhere.
    """
    try:
        if not is_copied_out(entry):
            admin = admin_dir(entry)
            if admin is None:
                return _keep_for_a_person(entry, "no copied-out record and no readable .git")
            reason = status_skip_reason(
                entry, worktree_git_env(repo, admin, entry), allow_deletions=True,
            )
            if reason is not None:
                return _keep_for_a_person(entry, f"no copied-out record and its tree is {reason}")
        removed = git(
            repo, ["worktree", "remove", "--force", str(entry)],
            timeout=WORKTREE_REMOVE_TIMEOUT_SECONDS, git_env=common_git_env(repo),
        )
        if removed.returncode == 0:
            _drop_record(entry)
            return True
        if not _not_a_worktree(entry, removed.stderr or removed.stdout):
            # A refusal mid-delete (``failed to delete``) drops the registration
            # anyway, so the next sweep finds the rest known to Git nowhere.
            return _keep_for_a_person(entry, f"git refused: {git_detail(removed)}")
        return _delete_unregistered(repo, entry)
    except (OSError, ReaperGitError) as exc:
        logger.warning("worktree_reaper: could not finish entry {}: {}", entry.name, exc)
        return False


def _delete_unregistered(repo: Path, entry: Path) -> bool:
    if not delete_quarantined(repo, entry):
        return False
    _drop_record(entry)
    return True


def sweep_quarantine(repo: Path) -> int:
    """Finish what earlier passes left in the quarantine; return how many went.

    It sorts the entries into three cases:

    * still claimed by Git under the path it left, a move cut off before its
      registry update: the tree's own check decides, see ``_finish_stranded_move``;
    * registered at this path, a removal that was cut off or refused: finished
      only on positive evidence, see ``_finish_removal``;
    * known to Git nowhere: its admin directory is gone, meaning the removal
      finished and only the directory was left.

    A copied-out record whose entry is gone is deleted; that can only strand an
    entry, because no deletion here depends on a record being absent.
    """
    root = quarantine_root(repo)
    if not root.is_dir() or root.is_symlink():
        return 0
    registered = {entry.path for entry in list_worktrees(repo)}
    removed = 0
    for entry in sorted(root.iterdir()):
        if entry.name.endswith(COPIED_OUT_SUFFIX):
            if not os.path.lexists(entry.with_name(entry.name[: -len(COPIED_OUT_SUFFIX)])):
                entry.unlink(missing_ok=True)
            continue
        if _stranded_move(entry):
            finished = _finish_stranded_move(repo, entry)
        elif entry.resolve(strict=False) in registered:
            finished = _finish_removal(repo, entry)
        else:
            finished = _delete_unregistered(repo, entry)
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
    moved = git(repo, ["worktree", "move", str(worktree), str(target)])
    if moved.returncode != 0:
        return "move_failed"
    if salvage is not None:
        try:
            salvage(target)
        except SalvageError:
            restored = git(repo, ["worktree", "move", str(target), str(worktree)])
            if restored.returncode != 0:
                # Registered here with no copied-out record: the sweep keeps it.
                logger.warning(
                    "worktree_reaper: {} left in quarantine after a failed salvage; move back: {}",
                    name, git_detail(restored),
                )
            raise
    if force:
        # The removal below deletes whatever is left, and nothing left is owed: the
        # work is copied out, or the checks found none. A non-forced removal gets
        # no record, so a file that races in meets Git's refusal and the sweep's
        # tree check, never a record that says it may go.
        mark_copied_out(target)
    # Without --force Git spawns its own status in the worktree; the child
    # inherits this pin but finds its GIT_DIR through the worktree. Deleting a
    # real tree takes minutes, so this call answers to the removal timeout.
    removed = git(
        repo, ["worktree", "remove", *(["--force"] if force else []), str(target)],
        timeout=WORKTREE_REMOVE_TIMEOUT_SECONDS, git_env=common_git_env(repo),
    )
    if removed.returncode == 0:
        _drop_record(target)
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
