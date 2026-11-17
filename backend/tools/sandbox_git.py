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

    Use for already-resolved paths, and for the fixed ``<resolved repo>/.worktrees``
    boundary, which is intentionally lexical: resolving that child could turn a
    symlink into a grant outside the repository. Other paths must be resolved
    first (``_sandbox_path``).
    """
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def worktree_folder_rules(repo: Path) -> list[str]:
    """Constant denies for a protected base's ``.worktrees``, at any worktree count.

    Each pattern is the exact literal subtree intersected with an any-case,
    depth-anchored regex. The literal pins the repository spelling (the regex
    root uses ``sandbox_any_case`` wildcards, never escapes), and the regex pins
    the depth, so neither a similarly named sibling nor a deeper ``.worktrees``
    matches. The folder and its immediate entries are pinned, so a ``.git``
    prepared deeper cannot be renamed into place, and every immediate entry's
    ``.git`` (file, directory, any case) stays unwritable. An immediate entry
    keeps only its own metadata writable (times, mode, xattrs, flags, owner,
    ACL), so ``touch .`` or a copy that preserves attributes still works at a
    worktree root. Creating, removing or renaming an entry checks other
    operations, which stay denied.
    """
    trees = repo / ".worktrees"
    subtree = f'(subpath "{sandbox_literal(trees)}")'
    pattern = sandbox_any_case(trees)

    def rule(effect: str, regex: str) -> str:
        return f'({effect} (require-all {subtree} (regex #"^{pattern}/{regex}")))'

    return [
        f'(deny file-write* (literal "{sandbox_literal(trees)}"))',
        rule("deny file-write*", "[^/]+$"),
        rule(f"allow {' '.join(ENTRY_METADATA_WRITES)}", "[^/]+$"),
        rule("deny file-write*", "[^/]+/[.][Gg][Ii][Tt](/|$)"),
    ]


# Seatbelt operations that change an existing entry's own metadata, not its name.
ENTRY_METADATA_WRITES = (
    "file-write-times", "file-write-mode", "file-write-xattr",
    "file-write-flags", "file-write-owner", "file-write-acl",
)


def worktree_pointer_rules(worktrees: list[Path]) -> list[str]:
    """One literal ``.git`` deny per worktree the listing proved, as
    unprotected repositories have always had."""
    return [f'(deny file-write* (literal "{sandbox_literal(tree / ".git")}"))' for tree in worktrees]


def sandbox_any_case(path: Path) -> str:
    """Spell ``path`` for a Seatbelt regex, which has no case-insensitive flag.

    A literal rule misses a differently cased name that doesn't exist yet. Other
    ASCII punctuation matches any one character, so an odd path can over-deny a
    sibling but can't break the profile.
    """
    def spell(char: str) -> str:
        if char == ".":
            return "[.]"
        if char.isascii() and char.isalpha():
            return f"[{char.upper()}{char.lower()}]"
        return char if not char.isascii() or char.isdigit() or char in "/-_ " else "."
    return "".join(spell(char) for char in str(path))


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
