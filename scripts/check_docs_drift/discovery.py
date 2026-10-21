"""Shared path/repo discovery helpers for the docs-drift checks."""

from __future__ import annotations

from pathlib import Path


def repo_root() -> Path:
    """Return the repository root (parent of this package's ``scripts/`` dir)."""
    return Path(__file__).resolve().parent.parent.parent


def top_level_names(root: Path) -> set[str]:
    """Return the real top-level file/directory names of ``root``."""
    return {entry.name for entry in root.iterdir()}
