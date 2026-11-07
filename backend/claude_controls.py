"""Shared Claude Code controls for confined child processes."""

from __future__ import annotations

#: The permission mode every confined child runs with; launch records store it from here.
CONFINED_PERMISSION_MODE = "dontAsk"


def confined_claude_controls(*, mcp_tools: bool = False) -> list[str]:
    """Return controls that prevent a confined child from inheriting local state.

    ``--safe-mode`` also drops servers named by ``--mcp-config``, so a launch whose
    tools come from an MCP server leaves it out; ``--setting-sources ""`` still keeps
    CLAUDE.md and project settings out.
    """
    return [
        "--restricted",
        *([] if mcp_tools else ["--safe-mode"]),
        "--setting-sources", "",
        "--strict-mcp-config",
        "--permission-mode", CONFINED_PERMISSION_MODE,
    ]
