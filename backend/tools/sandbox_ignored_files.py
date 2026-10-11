"""Git's ignored-and-untracked classification for a sandbox write."""

from __future__ import annotations

import subprocess
from pathlib import Path


def is_ignored_untracked_file(path: Path, repo: Path) -> bool:
    """Return Git's exact ignored-and-not-tracked classification for a file."""
    if path.is_dir():
        return False
    try:
        relative = path.relative_to(repo)
    except ValueError:
        return False
    if not relative.parts or relative.parts[0] == ".git":
        return False
    try:
        ignored = subprocess.run(
            ["git", "check-ignore", "--quiet", "--", str(relative)],
            cwd=str(repo), capture_output=True, text=True, timeout=5,
        )
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", str(relative)],
            cwd=str(repo), capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False
    return ignored.returncode == 0 and tracked.returncode == 1
