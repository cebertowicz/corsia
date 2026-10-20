"""Reach checks for MCP tools bound to durable connected-app identities."""

import sqlite3
from typing import Any

from tools.base import Tool


def mcp_app_id(tool: Tool) -> str | None:
    """Return the stable connected-app identity bound to one MCP tool."""
    app_id = getattr(tool, "mcp_app_id", None)
    return app_id if isinstance(app_id, str) and app_id else None


def mcp_tool_reaches_thread(tool: Tool, thread_id: str, *, db: Any) -> bool:
    """Apply the connected-app reach owner to one app-bound MCP tool."""
    app_id = mcp_app_id(tool)
    if app_id is None:
        return False
    from connections.app_reach import app_reaches_thread

    return app_reaches_thread(app_id, thread_id, db=db)


def connected_app_reach_refusal() -> str:
    """The value-free remedy for a connected app outside the current reach."""
    return (
        "Error: This connected app is not available to this project. Ask the "
        "person who manages Connected apps to grant this project access, then retry."
    )


def mcp_tool_visible_here(tool: Tool) -> bool:
    """Check an app-bound tool against this server's current thread context."""
    if mcp_app_id(tool) is None:
        return False
    from infra import state
    from infra.database import get_db

    thread_id = state._current_thread_id.get(None)
    if not isinstance(thread_id, str) or not thread_id:
        return False
    db = get_db()
    try:
        return mcp_tool_reaches_thread(tool, thread_id, db=db)
    except (OSError, sqlite3.Error):
        # Discovery is fail-closed when the reach store is unavailable.
        return False
    finally:
        db.close()
