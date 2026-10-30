"""Workspace Git files an agent shell must never change.

The backend runs Git in the workspace outside the sandbox, so these paths stay
read-only to agent shells.
"""

from __future__ import annotations

from pathlib import Path


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
    With no repository yet the paths still apply: the backend adopts any ``.git``
    it finds, so an agent must not be able to make one.
    """
    if workspace is None:
        return ()
    marker = workspace.resolve(strict=False) / ".git"
    git_dir = _git_pointer(marker, marker.parent, "gitdir:") if marker.is_file() else marker.resolve()
    common = _git_pointer(git_dir / "commondir", git_dir)
    return (marker, common / "config", common / "config.lock", git_dir / "config.worktree",
            git_dir / "commondir", common / "hooks")


def is_workspace_git_control(workspace: Path | None, path: Path) -> bool:
    """Return whether ``path`` is a control path, ignoring case as a default macOS volume does."""
    paths = [str(item).casefold() for item in workspace_git_paths(workspace)]
    name = str(path).casefold()
    return name in paths or (bool(paths) and name.startswith(f"{paths[-1]}/"))


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


def workspace_git_rules(paths: tuple[Path, ...]) -> list[str]:
    """Render Seatbelt denies for ``workspace_git_paths`` output."""
    if not paths:
        return []
    exact = [f'(deny file-write* (regex #"^{_any_case(path)}$"))' for path in paths[:-1]]
    return [*exact, f'(deny file-write* (regex #"^{_any_case(paths[-1])}(/|$)"))']
