"""Workspace Git metadata paths and sandbox rules.

The backend runs workspace Git outside the sandbox. Agent shells and file tools
must not read or write that metadata.
"""

from __future__ import annotations

import os
from pathlib import Path

from tools.sandbox_git import sandbox_any_case, sandbox_literal


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
    """Resolve Git locations once; an invalid pointer resolves to ``invalid_marker``."""
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


def workspace_git_history_paths(workspace: Path | None) -> tuple[Path, ...]:
    """Return the marker, git directory and common directory, without invoking Git.

    Read from disk, never by running Git, so a broken repository keeps its rules.
    With no repository yet the marker still applies: the backend adopts any ``.git``
    it finds, so an agent must not be able to make one.
    """
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


def workspace_git_history_rules(workspace: Path | None) -> list[str]:
    """Deny reads and writes throughout the workspace's Git metadata."""
    return [
        rule
        for path in workspace_git_history_paths(workspace)
        for rule in (
            f'(deny file-read* file-write* (subpath "{sandbox_literal(path)}"))',
            f'(deny file-read* file-write* (regex #"^{sandbox_any_case(path)}(/|$)"))',
        )
    ]


def workspace_git_rules(paths: tuple[Path, ...]) -> list[str]:
    """Render Seatbelt write denies for Git control paths; the last one is a directory."""
    if not paths:
        return []
    exact = [f'(deny file-write* (regex #"^{sandbox_any_case(path)}$"))' for path in paths[:-1]]
    return [*exact, f'(deny file-write* (regex #"^{sandbox_any_case(paths[-1])}(/|$)"))']
