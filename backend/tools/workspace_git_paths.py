"""Workspace Git files an agent shell must never change.

The backend runs Git in the workspace outside the sandbox, so these paths stay
read-only to agent shells.
"""

from __future__ import annotations

from pathlib import Path

from tools.sandbox_git import sandbox_literal


def _git_pointer(path: Path, base: Path, prefix: str = "") -> Path:
    """Resolve a Git pointer file against ``base``; with no usable file, ``base`` itself."""
    try:
        text = path.read_text().strip()
    except (OSError, UnicodeDecodeError):
        return base
    if not text.startswith(prefix):
        return base
    return (base / text.removeprefix(prefix).strip()).resolve(strict=False)


def workspace_git_paths(workspace: Path | None) -> tuple[Path, ...]:
    """Return the Git marker and control paths, ending with the hooks directory.

    Read from disk, never by running Git, so a broken repository keeps its rules.
    """
    if workspace is None:
        return ()
    marker = workspace.resolve(strict=False) / ".git"
    if not marker.exists():
        return ()
    git_dir = _git_pointer(marker, marker.parent, "gitdir:") if marker.is_file() else marker.resolve()
    common = _git_pointer(git_dir / "commondir", git_dir)
    return (marker, common / "config", common / "config.lock", git_dir / "config.worktree",
            git_dir / "commondir", common / "hooks")


def is_workspace_git_control(workspace: Path | None, path: Path) -> bool:
    """Return whether ``path`` is a control path, ignoring case as a default macOS volume does."""
    paths = [str(item).casefold() for item in workspace_git_paths(workspace)]
    name = str(path).casefold()
    return name in paths or (bool(paths) and name.startswith(f"{paths[-1]}/"))


def workspace_git_rules(paths: tuple[Path, ...]) -> list[str]:
    """Render Seatbelt denies for ``workspace_git_paths`` output."""
    if not paths:
        return []
    literals = [f'(deny file-write* (literal "{sandbox_literal(path)}"))' for path in paths[:-1]]
    return [*literals, f'(deny file-write* (subpath "{sandbox_literal(paths[-1])}"))']
