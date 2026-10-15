"""One tool catalog entry: the product metadata the catalog declares per tool."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ToolStatus = Literal["active", "substrate", "admin", "suspect", "retired"]
ToolCategory = Literal[
    "filesystem",
    "edit",
    "shell",
    "web",
    "browser",
    "history",
    "memory",
    "projects",
    "coordination",
    "delegation",
    "plans",
    "automations",
    "documents",
    "artifacts",
    "connection_admin",
    "host_access",
    "discovery",
]


@dataclass(frozen=True)
class ToolCatalogEntry:
    """Product metadata for one registry tool.

    ``authority`` is descriptive classification for audit/review surfaces. Tool
    enforcement remains in each tool's implementation or its backing service.
    A ``core`` tool is advertised with its full schema on every call; every
    other tool is listed by ``summary`` in its pack's line of the
    ``tools_load`` index and reached through ``tools_load``/``tool_call``.
    """

    category: ToolCategory
    status: ToolStatus = "active"
    external_harness: bool = True
    authority: str = "session"
    core: bool = False
    summary: str = ""
    # The row verb a call of this tool reads as, e.g. "Reading" in "Reading a.md".
    activity: str = field(kw_only=True)
