"""Shared Claude Code controls for confined child processes."""

from __future__ import annotations


def confined_claude_controls() -> list[str]:
    """Return controls that prevent a confined child from inheriting local state."""
    return [
        "--restricted",
        "--safe-mode",
        "--setting-sources", "",
        "--strict-mcp-config",
        "--permission-mode", "dontAsk",
    ]
