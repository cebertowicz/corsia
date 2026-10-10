"""Shared path/repo discovery helpers for the docs-drift checks."""

from __future__ import annotations

import os
from pathlib import Path

# Not the maintainer's literal home directory: resolved from the current
# user at runtime so this default never puts a real username into the
# repository this script ships in (the exact leak check 3 looks for).
DEFAULT_WORKSPACE = Path.home() / "corsia"


def repo_root() -> Path:
    """Return the repository root (parent of this package's ``scripts/`` dir)."""
    return Path(__file__).resolve().parent.parent.parent


def resolve_workspace(explicit: str | None) -> Path | None:
    """Return the workspace root to diff starter-kit copies against.

    Resolution order: ``--workspace``, then ``CORSIA_WORKSPACE``, then the
    default path. Returns ``None`` when nothing on that chain exists on
    disk, which callers treat as "no workspace available" (an outside
    contributor's clone), not an error.
    """
    candidate = explicit or os.environ.get("CORSIA_WORKSPACE") or str(DEFAULT_WORKSPACE)
    path = Path(candidate).expanduser()
    return path if path.is_dir() else None


def top_level_names(root: Path) -> set[str]:
    """Return the real top-level file/directory names of ``root``."""
    return {entry.name for entry in root.iterdir()}
