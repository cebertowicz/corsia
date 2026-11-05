"""Workspace Git metadata paths and sandbox rules.

The backend runs workspace Git outside the sandbox. Agent shells and file tools
must not read or write that metadata.
"""

from __future__ import annotations

import os
from pathlib import Path

from tools.sandbox_git import sandbox_literal


def _git_pointer(path: Path, base: Path, prefix: str = "") -> Path | None:
    """Read a Git pointer relative to its base, or return None when invalid."""
    try:
        text = path.read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not text.startswith(prefix):
        return None
    value = text.removeprefix(prefix).strip()
    if prefix and not value:
        return None
    return base / value


def _workspace_git_locations(workspace: Path, *, invalid_marker: Path) -> tuple[Path, Path, Path]:
    """Resolve Git locations once, using the consumer's invalid-marker fallback."""
    marker = workspace.resolve(strict=False) / ".git"
    if marker.is_file():
        git_dir = _git_pointer(marker, marker.parent, "gitdir:")
        git_dir = git_dir if git_dir is not None else invalid_marker
    else:
        git_dir = marker
    resolved_git_dir = git_dir.resolve(strict=False)
    common_file = resolved_git_dir / "commondir"
    common = _git_pointer(common_file, resolved_git_dir) if common_file.is_file() else None
    return marker, git_dir, common if common is not None else resolved_git_dir


def workspace_git_paths(workspace: Path | None) -> tuple[Path, ...]:
    """Return the Git marker and control paths, ending with the hooks directory.

    Read from disk, never by running Git, so a broken repository keeps its rules.
    With no repository yet the paths still apply: the backend adopts any ``.git``
    it finds, so an agent must not be able to make one.
    """
    if workspace is None:
        return ()
    marker, git_dir, common = _workspace_git_locations(
        workspace, invalid_marker=workspace.resolve(strict=False),
    )
    git_dir, common = git_dir.resolve(strict=False), common.resolve(strict=False)
    return (marker, common / "config", common / "config.lock", git_dir / "config.worktree",
            git_dir / "commondir", common / "hooks")


def workspace_git_history_paths(workspace: Path | None) -> tuple[Path, ...]:
    """Return the marker, git directory and common directory, without invoking Git."""
    if workspace is None:
        return ()
    lexical_marker = Path(os.path.abspath(workspace)) / ".git"
    marker, git_dir, common = _workspace_git_locations(workspace, invalid_marker=lexical_marker)
    return tuple(dict.fromkeys((lexical_marker, marker, marker.resolve(strict=False),
                               Path(os.path.abspath(git_dir)), git_dir.resolve(strict=False),
                               Path(os.path.abspath(common)), common.resolve(strict=False))))


def is_workspace_git_metadata(workspace: Path | None, path: Path) -> bool:
    """Return whether a path is in workspace Git metadata, ignoring case."""
    paths = [str(item).casefold() for item in workspace_git_history_paths(workspace)]
    name = str(path).casefold()
    return any(name == root or name.startswith(f"{root}/") for root in paths)


def _any_case(path: Path) -> str:
    """Spell ``path`` for a Seatbelt regex, which has no case-insensitive flag.

    A literal rule misses a differently cased name that doesn't exist yet. Other
    ASCII punctuation matches any one character, so an odd workspace path can
    over-deny a sibling but can't break the profile.
    """
    def spell(char: str) -> str:
        if char == ".":
            return "[.]"
        if char.isascii() and char.isalpha():
            return f"[{char.upper()}{char.lower()}]"
        return char if not char.isascii() or char.isdigit() or char in "/-_ " else "."
    return "".join(spell(char) for char in str(path))


def workspace_git_history_rules(workspace: Path | None) -> list[str]:
    """Deny reads and writes throughout the workspace's Git metadata."""
    return [
        rule
        for path in workspace_git_history_paths(workspace)
        for rule in (
            f'(deny file-read* file-write* (subpath "{sandbox_literal(path)}"))',
            f'(deny file-read* file-write* (regex #"^{_any_case(path)}(/|$)"))',
        )
    ]


def workspace_git_rules(paths: tuple[Path, ...]) -> list[str]:
    """Render Seatbelt denies for ``workspace_git_paths`` output."""
    if not paths:
        return []
    exact = [f'(deny file-write* (regex #"^{_any_case(path)}$"))' for path in paths[:-1]]
    return [*exact, f'(deny file-write* (regex #"^{_any_case(paths[-1])}(/|$)"))']
