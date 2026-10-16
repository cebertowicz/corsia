"""Pure linked-worktree Git-marker verification."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def _is_plain_file(path: Path) -> bool:
    """True for a regular file that is not a symlink (one ``lstat``)."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def sandbox_literal(path: Path) -> str:
    """Escape ``path`` for a Safehouse profile rule exactly as given, no resolve.

    Precondition: a root ``list_repo_worktrees`` returned in this request, or
    ``<root>/.git`` after ``verified_worktree_marker`` proved it a non-symlink
    regular file. Any other path must be resolved first (``_sandbox_path``).
    """
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def verified_worktree_marker(
    worktree: Path, repo: Path,
) -> tuple[Path, Path] | None:
    """Prove ``worktree`` is linked to ``repo`` through reciprocal Git markers."""
    marker = worktree / ".git"
    if not _is_plain_file(marker):
        return None
    try:
        prefix, raw_gitdir = marker.read_text(encoding="utf-8").strip().split(":", 1)
    except (OSError, ValueError):
        return None
    if prefix != "gitdir":
        return None
    gitdir = Path(raw_gitdir.strip()).expanduser()
    resolved_gitdir = (
        gitdir if gitdir.is_absolute() else marker.parent / gitdir
    ).resolve(strict=False)
    try:
        resolved_gitdir.relative_to(repo / ".git" / "worktrees")
    except ValueError:
        return None
    reciprocal = resolved_gitdir / "gitdir"
    if not _is_plain_file(reciprocal):
        return None
    try:
        reciprocal_marker = Path(
            reciprocal.read_text(encoding="utf-8").strip()
        ).expanduser()
    except OSError:
        return None
    if not reciprocal_marker.is_absolute():
        reciprocal_marker = reciprocal.parent / reciprocal_marker
    # ``marker`` was just proved a non-symlink file, so it resolves to the
    # resolved worktree plus ``.git``; resolve the worktree only once.
    resolved_worktree = worktree.resolve(strict=False)
    if reciprocal_marker.resolve(strict=False) != resolved_worktree / ".git":
        return None
    return resolved_worktree, resolved_gitdir
