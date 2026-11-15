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

    Use for already-resolved paths and verified worktree markers. The fixed
    ``<resolved repo>/.worktrees`` boundary is intentionally lexical: resolving
    that child could turn a symlink into a grant outside the repository.
    Other paths must be resolved first (``_sandbox_path``).
    """
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def sandbox_any_case(path: Path, *, exact: bool = False) -> str:
    """Spell ``path`` for a Seatbelt regex, which has no case-insensitive flag.

    A literal rule misses a differently cased name that doesn't exist yet. Other
    ASCII punctuation matches any one character, so an odd path can over-deny a
    sibling but can't break the profile. ``exact`` escapes metacharacters and
    the Seatbelt string instead, for boundaries that must not affect siblings.
    """
    def spell(char: str) -> str:
        if char == ".":
            return "[.]"
        if char.isascii() and char.isalpha():
            return f"[{char.upper()}{char.lower()}]"
        if exact:
            if char == '"':
                return r"\x22"
            return f"\\{char}" if char in r"\^$*+?{}[]|()" else char
        return char if not char.isascii() or char.isdigit() or char in "/-_ " else "."
    pattern = "".join(spell(char) for char in str(path))
    return pattern.replace("\\", "\\\\").replace('"', '\\"') if exact else pattern


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
