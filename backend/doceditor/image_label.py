"""How a refusal names an image."""

from __future__ import annotations

from pathlib import Path

from core.workspace_layout import resolve_workspace_root
from infra import config


def image_label(path: str | Path) -> str:
    """How a refusal names an image: its path from the workspace root, never a path on this machine."""
    resolved = Path(path).expanduser().resolve(strict=False)
    root = resolve_workspace_root(config.WORKSPACE).resolve(strict=False)
    return resolved.relative_to(root).as_posix() if resolved.is_relative_to(root) else resolved.name
