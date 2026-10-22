"""Shared Claude Code controls for confined child processes."""

from __future__ import annotations

#: The permission mode every confined child runs with; launch records store it from here.
CONFINED_PERMISSION_MODE = "dontAsk"


def confined_claude_controls() -> list[str]:
    """Return controls that prevent a confined child from inheriting local state."""
    return [
        "--restricted",
        "--safe-mode",
        "--setting-sources", "",
        "--strict-mcp-config",
        "--permission-mode", CONFINED_PERMISSION_MODE,
    ]
