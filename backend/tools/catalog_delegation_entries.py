"""Catalog metadata for the delegation pack's agent-control tools."""

from __future__ import annotations

from tools.catalog_entry import ToolCatalogEntry

_Entry = ToolCatalogEntry


DELEGATION_CATALOG_ENTRIES: dict[str, ToolCatalogEntry] = {
    "delegate_create_thread": _Entry(
        "delegation", status="substrate",
        summary="Create a visible chat or a hidden subagent thread",
        activity="Creating thread",
    ),
    "delegate_send": _Entry(
        "delegation", status="substrate",
        summary="Send a message into another thread",
        activity="Messaging",
    ),
    "delegate_status": _Entry(
        "delegation", status="substrate",
        summary="Read a delegated thread's status and recent messages",
        activity="Checking agent",
    ),
    "delegate_set_model": _Entry(
        "delegation", status="substrate", authority="model-switch",
        summary="Change a running thread's model or harness in place",
        activity="Switching model",
    ),
    "delegate_stop": _Entry(
        # In the plans pack: the delegation pack is at the ten-tool limit a pack may hold.
        "plans", status="substrate",
        summary="Stop a worker you spawned, and the workers beneath it",
        activity="Stopping worker",
    ),
    "delegate_task": _Entry(
        "delegation", status="substrate",
        summary="Delegate a self-contained task to a new subagent",
        activity="Delegating",
    ),
}
