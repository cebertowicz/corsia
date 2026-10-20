"""Agent-facing tool catalog: metadata, packs, and the one advertised surface."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, TypeGuard

# The catalog's data module; this module is its public import path.
from tools.catalog_entries import TOOL_CATALOG
from tools.catalog_entry import ToolCatalogEntry

if TYPE_CHECKING:
    from tools.registry import ToolRegistry


# Pack id -> index heading, in index order. A non-core tool's pack is its
# category's pack, with ``host_access`` named ``host`` on the tool surface.
TOOL_PACKS: dict[str, str] = {
    "plans": "Plans",
    "delegation": "Delegation and agents",
    "artifacts": "Artifacts",
    "documents": "Document comments",
    "history": "History",
    "memory": "Knowledge and lessons",
    "projects": "Projects and repositories",
    "coordination": "Coordination setup: CORSIA API and instruction files",
    "host": "Host access, audit, and processes",
    "browser": "Browser",
    "automations": "Automations",
    "web": "Web",
    "google": "Email and calendar",
    "microsoft": "Microsoft sign-in",
    "microsoft_mail": "Microsoft mail",
    "microsoft_calendar": "Microsoft calendar",
    "microsoft_files": "Microsoft files",
}
_CATEGORY_PACKS: dict[str, str] = {
    "plans": "plans",
    "delegation": "delegation",
    "artifacts": "artifacts",
    "documents": "documents",
    "history": "history",
    "memory": "memory",
    "projects": "projects",
    "coordination": "coordination",
    "host_access": "host",
    "browser": "browser",
    "automations": "automations",
    "web": "web",
    "google": "google",
    "microsoft": "microsoft",
    "microsoft_mail": "microsoft_mail",
    "microsoft_calendar": "microsoft_calendar",
    "microsoft_files": "microsoft_files",
}


# Claude Code's built-in tools carry their own names rather than our
# catalog's; map the ones with a canonical native equivalent here. This is
# the harness-variant knowledge the rest of the system should never need —
# match against ``normalize_tool_name``'s output instead of enumerating
# per-harness names elsewhere.
# CORSIA launches Claude Code with `--tools mcp__corsia__*` and
# `--strict-mcp-config` (see ``subagents/cc_invocation.build_claude_cmd``),
# so these builtins are never reachable live; this map exists only to
# normalize names already recorded in transcript and checkpoint material.
CC_BUILTIN_ALIASES: dict[str, str] = {
    "Bash": "bash",
    "Write": "write_file",
    "Edit": "edit_file",
    "MultiEdit": "multi_edit",
    "Read": "read_file",
}

#: How Claude Code names every CORSIA tool; the launch and any text that
#: names a tool to a Claude Code agent derive from this one value.
MCP_CORSIA_PREFIX = "mcp__corsia__"


def normalize_tool_name(name: str | None) -> str:
    """Map a harness-emitted tool name to its canonical ``TOOL_CATALOG`` name.

    Collapses the two sources of naming variance — the ``mcp__corsia__``
    MCP-over-native prefix and Claude Code's builtin aliases — into one
    seam. An already-canonical name, or a name with no known mapping,
    passes through unchanged.
    """
    if not name:
        return ""
    if name.startswith(MCP_CORSIA_PREFIX):
        name = name[len(MCP_CORSIA_PREFIX):]
    return CC_BUILTIN_ALIASES.get(name, name)


def missing_catalog_entries(tool_names: list[str]) -> list[str]:
    """Return registered tool names missing catalog metadata."""
    return sorted(name for name in tool_names if name not in TOOL_CATALOG)


def stale_catalog_entries(tool_names: list[str]) -> list[str]:
    """Return catalog rows that do not map to registered tools."""
    registered = set(tool_names)
    return sorted(name for name in TOOL_CATALOG if name not in registered)


def _reachable(entry: ToolCatalogEntry | None) -> TypeGuard[ToolCatalogEntry]:
    return (
        entry is not None and entry.external_harness and entry.status != "retired"
    )


def is_reachable(name: str) -> bool:
    """Return whether a harness may load or dispatch this tool by name."""
    return _reachable(TOOL_CATALOG.get(name))


def tool_pack(name: str) -> str | None:
    """Return the pack id of a reachable non-core tool, else ``None``."""
    entry = TOOL_CATALOG.get(name)
    if not _reachable(entry) or entry.core:
        return None
    return _CATEGORY_PACKS[entry.category]


def pack_members() -> dict[str, tuple[str, ...]]:
    """Return every pack's tool names, packs in index order, names sorted."""
    members: dict[str, list[str]] = {pack: [] for pack in TOOL_PACKS}
    for name in sorted(TOOL_CATALOG):
        pack = tool_pack(name)
        if pack is not None:
            members[pack].append(name)
    return {pack: tuple(names) for pack, names in members.items()}


def advertised_tool_definitions(registry: ToolRegistry) -> tuple[dict, ...]:
    """Return the one provider-facing tool list for every harness.

    Native, Claude Code (MCP bridge) and Codex (dynamic tools) all send
    exactly this: registered core tools and explicitly always-loaded configured
    MCP tools, sorted by name, each a detached copy. Other tools stay reachable
    through ``tools_load`` and ``tool_call``, so the list changes only when the
    registry does: an MCP approved connected-app runtime changes are the only
    source of such updates.
    """
    definitions = []
    for definition in registry.get_definitions():
        name = definition["function"]["name"]
        entry = TOOL_CATALOG.get(name)
        if entry is not None:
            if not (_reachable(entry) and entry.core):
                continue
        else:
            tool = registry.get(name)
            if not (tool is not None and tool.user_configured and tool.always_advertised):
                continue
        definitions.append(deepcopy(definition))
    definitions.sort(key=lambda definition: definition["function"]["name"])
    return tuple(definitions)


def fork_tool_definitions(
    registry: ToolRegistry, tool_allowlist: frozenset[str],
) -> tuple[dict, ...]:
    """Disclose only a disposable fork's fixed tools through the catalog boundary."""
    source = (
        registry.get_definitions()
        if hasattr(registry, "get_definitions") else advertised_tool_definitions(registry)
    )
    definitions = [
        deepcopy(definition)
        for definition in source
        if definition["function"]["name"] in tool_allowlist
    ]
    definitions.sort(key=lambda definition: definition["function"]["name"])
    return tuple(definitions)
