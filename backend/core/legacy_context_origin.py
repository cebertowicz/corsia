"""Compatibility normalization for context rows stored before workspace rename."""

from __future__ import annotations

from typing import Any, Literal

ContextScope = Literal["workspace", "project", "thread"]

LEGACY_WORKSPACE_SOURCE_PREFIXES = (
    "system-bootstrap:",
    "native:system:bootstrap",
)


def normalize_legacy_row(row: dict[str, Any]) -> dict[str, Any]:
    """Map only legacy workspace bootstrap rows to the workspace origin.

    Stored runtime-envelope and Claude Code seed rows also use ``system`` and
    must retain that origin. The returned copy leaves the persisted snapshot
    unchanged when restored sources are subsequently retained.
    """
    if (
        row.get("origin_section") == "system"
        and str(row.get("source_id", "")).startswith(
            LEGACY_WORKSPACE_SOURCE_PREFIXES
        )
    ):
        return {**row, "origin_section": "workspace"}
    return row


def normalize_skill_scope(scope: str) -> ContextScope:
    """Normalize the one-release legacy global scope to workspace."""
    if scope in {"global", "workspace"}:
        return "workspace"
    if scope == "project":
        return "project"
    if scope == "thread":
        return "thread"
    raise ValueError("scope must be workspace (or legacy global), project, or thread")
