"""Pure linked-worktree Git-marker verification."""

from __future__ import annotations

from pathlib import Path


def verified_worktree_marker(
    worktree: Path, repo: Path, deps: list[Path] | None = None,
) -> tuple[Path, Path] | None:
    """Prove ``worktree`` is linked to ``repo`` through reciprocal Git markers.

    When ``deps`` is given, every *unresolved* path the verdict reads or
    resolves is appended to it, so a caller can stamp each of them and every
    ancestor (an intermediate symlink swap changes the resolved result).
    """
    marker = worktree / ".git"
    if deps is not None:
        deps.append(marker)
    if marker.is_symlink() or not marker.is_file():
        return None
    try:
        prefix, raw_gitdir = marker.read_text(encoding="utf-8").strip().split(":", 1)
    except (OSError, ValueError):
        return None
    if prefix != "gitdir":
        return None
    gitdir = Path(raw_gitdir.strip()).expanduser()
    raw_gitdir_path = gitdir if gitdir.is_absolute() else marker.parent / gitdir
    resolved_gitdir = raw_gitdir_path.resolve(strict=False)
    if deps is not None:
        deps.extend((raw_gitdir_path, resolved_gitdir))
    try:
        resolved_gitdir.relative_to(repo / ".git" / "worktrees")
    except ValueError:
        return None
    reciprocal = resolved_gitdir / "gitdir"
    if deps is not None:
        deps.append(reciprocal)
    if reciprocal.is_symlink() or not reciprocal.is_file():
        return None
    try:
        reciprocal_marker = Path(
            reciprocal.read_text(encoding="utf-8").strip()
        ).expanduser()
    except OSError:
        return None
    if not reciprocal_marker.is_absolute():
        reciprocal_marker = reciprocal.parent / reciprocal_marker
    if deps is not None:
        deps.append(reciprocal_marker)
    if reciprocal_marker.resolve(strict=False) != marker.resolve(strict=False):
        return None
    return worktree.resolve(strict=False), resolved_gitdir
