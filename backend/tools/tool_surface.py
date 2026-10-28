"""Progressive tool disclosure: the pack index, ``tools_load`` and ``tool_call``.

Every harness advertises the same small, byte-stable tool list
(``tools.catalog.advertised_tool_definitions``). Every other registered tool
stays reachable through two always-advertised tools: ``tools_load``, whose
description carries a one-line index of each such tool by pack and whose
result is the full schemas, and ``tool_call``, which calls any tool by name.
``ToolRegistry`` unwraps a ``tool_call`` before its normal execution path, so
validation, permissions, plan dispatch records and telemetry all see the real
tool. The provider transcript keeps the call exactly as the model emitted it;
result rows record the real tool as ``tool_name`` beside ``dispatcher``.
Both reach only catalog-reachable tools, the rule the advertised list uses,
plus the tools of the user's configured MCP servers (``Tool.user_configured``),
which the index lists by name. Any other uncatalogued or non-external
registered tool stays unreachable.
"""

from __future__ import annotations

import difflib
import json
import re
from collections import Counter
from typing import TYPE_CHECKING, Any

from tools.base import Tool
from tools.connected_app_visibility import (
    connected_app_reach_refusal,
    mcp_app_id,
    mcp_tool_reaches_thread,
    mcp_tool_visible_here,
    tools_for_connected_accounts,
)
from tools.catalog import (
    TOOL_CATALOG,
    TOOL_PACKS,
    is_reachable,
    normalize_tool_name,
    pack_members,
    tool_pack,
)

if TYPE_CHECKING:
    from tools.registry import ToolRegistry
TOOLS_LOAD = "tools_load"
TOOL_CALL = "tool_call"
_LOAD_HEADER = (
    "Load the full schemas of CORSIA tools that are not in your tool list, "
    "then call them with tool_call. Pass pack to load a whole pack, or names "
    "for specific tools. A loaded schema stays valid for the whole thread, so "
    "load each tool once. Every tool not in your tool list is listed below, "
    "grouped by pack.\n"
)


def resolve_dispatch(
    name: str, params: Any,
) -> tuple[str, Any, str | None]:
    """Return ``(tool name, tool arguments, dispatcher)`` for one call.

    A well-formed ``tool_call`` resolves to the tool it names and that tool's
    own arguments, with ``tool_call`` as the dispatcher. Anything else,
    including a malformed ``tool_call`` that its own schema will reject,
    passes through with no dispatcher.
    """
    if normalize_tool_name(name) != TOOL_CALL or not isinstance(params, dict):
        return name, params, None
    target = params.get("name")
    arguments = params.get("arguments")
    if not isinstance(target, str) or not target or not isinstance(arguments, dict):
        return name, params, None
    return normalize_tool_name(target), arguments, TOOL_CALL


def dispatch_metadata(name: str, arguments: Any) -> dict[str, str]:
    """Return the result-row fields naming the real tool behind a dispatch."""
    tool_name, _arguments, dispatcher = resolve_dispatch(name, arguments)
    if dispatcher is None:
        return {}
    return {"tool_name": tool_name, "dispatcher": dispatcher}


_MCP_SUMMARY_CHARS = 120

# A person reads an app by its maker's name; unknown apps keep their configured name.
_APP_DISPLAY_NAMES = {
    "github": "GitHub", "filesystem": "Files", "playwright": "Playwright",
    "linear": "Linear", "notion": "Notion", "atlassian": "Atlassian",
    "stripe": "Stripe", "sentry": "Sentry", "postgres": "Postgres", "slack": "Slack",
}


def app_display_name(server: str) -> str:
    """The name the context sheet shows for a connected app."""
    return _APP_DISPLAY_NAMES.get(server.lower(), server)


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_CAMEL = re.compile(r"([a-z])([A-Z])")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s")
_PURPOSE_TOPICS = 6
# Generic verbs and filler that say nothing about what a server is for.
_PURPOSE_STOP = frozenset({
    "get", "list", "create", "update", "delete", "add", "remove", "read", "write",
    "search", "post", "patch", "put", "retrieve", "set", "edit", "move", "handle",
    "run", "show", "a", "an", "the", "of", "to", "or", "and", "by", "for", "in",
    "api", "mcp", "multiple", "all", "new", "request", "file", "with", "open",
})


def _one_line(value: str) -> str:
    """Control characters become spaces; the line is capped at the index width."""
    return _CONTROL.sub(" ", value).strip()[:_MCP_SUMMARY_CHARS]


def _first_line(tool: Tool) -> str:
    """A tool's first description line, as the index and the server listing show it."""
    lines = (tool.description or "").strip().splitlines()
    return _one_line(lines[0] if lines else tool.name)


def _server_tools(registry: ToolRegistry) -> dict[str, list[Tool]]:
    """Group the configured MCP tools by server, in each server's own discovery order."""
    grouped: dict[str, list[Tool]] = {}
    for tool in _user_configured_in_order(registry):
        if not mcp_tool_visible_here(tool):
            continue
        server = getattr(tool, "mcp_server_name", None)
        if isinstance(server, str) and server:
            grouped.setdefault(server, []).append(tool)
    return dict(sorted(grouped.items()))


def _topics(tools: list[Tool]) -> list[str]:
    """The words the server's own tool names repeat most: count, then first appearance."""
    counts: Counter[str] = Counter()
    first: dict[str, int] = {}
    for position, tool in enumerate(tools):
        name = getattr(tool, "mcp_tool_name", None) or tool.name
        for word in re.split(r"[_\-\s]+", _CAMEL.sub(r"\1_\2", name)):
            word = word.lower()
            if word and word not in _PURPOSE_STOP and len(word) >= 3:
                counts[word] += 1
                first.setdefault(word, position)
    return sorted(counts, key=lambda word: (-counts[word], first[word]))[:_PURPOSE_TOPICS]


def _server_purpose(name: str, tools: list[Tool]) -> str:
    """What one server is for: an override, its own instructions, one tool, then its names."""
    config = getattr(tools[0], "mcp_server_config", None) or {}
    override = config.get("purpose")
    if isinstance(override, str) and override.strip():
        return _one_line(override)
    instructions = getattr(tools[0], "mcp_instructions", None)
    if isinstance(instructions, str) and instructions.strip():
        return _one_line(_SENTENCE_END.split(instructions.strip(), maxsplit=1)[0])
    if len(tools) == 1:
        return _first_line(tools[0])
    return ", ".join(_topics(tools)) or _one_line(name)


def _server_label(name: str) -> str:
    return _CONTROL.sub("", name)


def pack_index(registry: ToolRegistry) -> str:
    """Render the always-on index: core packs plus one line per MCP server."""
    lines = []
    visible = set(tools_for_connected_accounts([name for names in pack_members().values() for name in names], TOOL_CATALOG))
    for pack, names in pack_members().items():
        names = [name for name in names if name in visible]
        if names:
            lines.append(f"[{pack}] {TOOL_PACKS[pack]}")
            lines.extend(f"- {name}: {TOOL_CATALOG[name].summary}" for name in names)
    servers = _server_tools(registry)
    if servers:
        lines.append('[mcp] Tool servers. Load one with tools_load(server="<name>") to see its tools.')
        lines.extend(
            f"- {_server_label(name)}: {len(tools)} tool{'s' if len(tools) != 1 else ''}; "
            f"{_server_purpose(name, tools)}"
            for name, tools in servers.items()
        )
    return "\n".join(lines)


def _user_configured_in_order(registry: ToolRegistry) -> list[Tool]:
    """Configured tools in registration order, which is each server's discovery order."""
    tools = (registry.get(name) for name in registry.tool_names)
    return [tool for tool in tools if tool is not None and tool.user_configured]


def _user_configured(registry: ToolRegistry) -> list[Tool]:
    tools = (registry.get(name) for name in sorted(registry.tool_names))
    return [tool for tool in tools if tool is not None and tool.user_configured]


def _hidden_mcp_tool(registry: ToolRegistry, name: str) -> bool:
    tool = registry.get(name)
    return bool(tool and tool.user_configured and not mcp_tool_visible_here(tool))


def _labelled(name: str) -> str:
    pack = tool_pack(name)
    return f"{name} (pack {pack})" if pack else name


def reachable_tool_names(
    registry: ToolRegistry, *, include_unreachable_mcp: bool = False,
) -> list[str]:
    """Return the registered tools ``tools_load`` and ``tool_call`` may reach."""
    configured = {
        tool.name
        for tool in _user_configured(registry)
        if include_unreachable_mcp or mcp_tool_visible_here(tool)
    }
    return [
        name for name in registry.tool_names
        if is_reachable(name) or name in configured
    ]


def unknown_tool_error(names: list[str], registered: list[str]) -> str:
    """Name close matches and the way to find a tool, never a bare refusal."""
    parts = []
    for name in names:
        matches = difflib.get_close_matches(name, registered, n=3, cutoff=0.6)
        parts.append(f"Error: Tool '{name}' not found.")
        if matches:
            parts.append(
                "Close matches: " + ", ".join(_labelled(m) for m in matches) + "."
            )
    parts.append(
        "Every tool is listed by pack in the tools_load description. Load its "
        "schema with tools_load(pack=...) or tools_load(names=[...]), then call "
        "it with tool_call(name, arguments). Packs: " + ", ".join(TOOL_PACKS) + "."
    )
    return " ".join(parts)


def invalid_dispatch_hint(tool: Tool) -> str:
    """Return the tool's own schema and pack for a rejected ``tool_call``."""
    pack = tool_pack(tool.name)
    where = f" (pack {pack})" if pack else ""
    schema = json.dumps(tool.parameters, ensure_ascii=False, sort_keys=True)
    return (
        f"\nParameters of {tool.name}{where}: {schema}\n"
        f'Retry with tool_call(name="{tool.name}", arguments={{...}}) '
        "using exactly these parameters."
    )


class ToolsLoadTool(Tool):
    """Return full provider-facing schemas for tools by pack or by name."""

    self_describing_errors = True

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    @property
    def name(self) -> str:
        return TOOLS_LOAD

    @property
    def description(self) -> str:
        return _LOAD_HEADER + pack_index(self._registry)

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pack": {
                    "type": "string",
                    "enum": list(TOOL_PACKS),
                    "description": "Load every tool in this pack.",
                },
                "server": {
                    "type": "string",
                    "description": "Load the per-tool index for one connected MCP server.",
                },
                "names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 20,
                    "description": "Load these tools by exact name.",
                },
            },
            "minProperties": 1,
            "additionalProperties": False,
        }

    async def execute(
        self, pack: str | None = None, names: list[str] | None = None,
        server: str | None = None, **_: Any,
    ) -> str:
        """Return requested schemas, or a connected server's compact tool index."""
        servers = _server_tools(self._registry)
        if server is not None:
            if any(
                getattr(tool, "mcp_server_name", None) == server
                and not mcp_tool_visible_here(tool)
                for tool in _user_configured(self._registry)
            ):
                return connected_app_reach_refusal()
            tools = servers.get(server)
            if tools is None:
                state = getattr(self._registry, "mcp_server_states", {}).get(server)
                if state:
                    return f"Error: MCP server '{server}' is not connected: {state}."
                known = ", ".join(servers) or "(none)"
                return f"Error: MCP server '{server}' is not connected. Known connected servers: {known}."
            lines = [
                f"Tools on {_server_label(server)}. Load by name with tools_load(names=[...]), "
                "then call with tool_call.",
            ]
            lines.extend(f"- {tool.name}: {_first_line(tool)}" for tool in tools)
            return "\n".join(lines)
        requested = list(pack_members().get(pack, ())) if pack else []
        for name in names or ():
            canonical = normalize_tool_name(name)
            if canonical not in requested:
                requested.append(canonical)
        if any(_hidden_mcp_tool(self._registry, name) for name in requested):
            return connected_app_reach_refusal()
        reachable = reachable_tool_names(self._registry)
        found = [name for name in requested if name in reachable]
        missing = [name for name in requested if name not in found]
        if not found:
            return unknown_tool_error(missing, reachable)
        schemas = [
            self._registry.get(name).to_schema()["function"] for name in found
        ]
        text = (
            f"Loaded {len(found)} tool(s). Call each with "
            'tool_call(name="<tool>", arguments={...}) using its parameters.\n'
            + json.dumps(schemas, ensure_ascii=False)
        )
        if missing:
            text += "\n" + unknown_tool_error(missing, reachable)
        return text


class ToolCallTool(Tool):
    """Advertise the by-name dispatcher; ``ToolRegistry`` performs dispatch.

    A well-formed call never reaches ``execute``: the registry resolves it to
    the named tool first (``resolve_dispatch``) and runs that tool through
    the ordinary path. A malformed call is rejected by this schema.
    """

    self_describing_errors = True

    @property
    def name(self) -> str:
        return TOOL_CALL

    @property
    def description(self) -> str:
        return (
            "Call any CORSIA tool by name, including tools listed only in the "
            "tools_load index. arguments must match that tool's parameters; "
            "load them with tools_load first. A wrong name or invalid "
            "arguments returns the correct schema to retry with."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Exact tool name, e.g. artifact_register.",
                },
                "arguments": {
                    "type": "object",
                    "description": (
                        "The tool's arguments as its schema describes; "
                        "{} when it takes none."
                    ),
                },
            },
            "required": ["name", "arguments"],
            "additionalProperties": False,
        }

    async def execute(self, name: Any = None, **_: Any) -> str:
        """Refuse a call the registry could not resolve to another tool."""
        if not isinstance(name, str) or not name.strip():
            return (
                "Error: tool_call needs the target tool's exact name, e.g. "
                'tool_call(name="artifact_register", arguments={...}). Every '
                "tool is listed in the tools_load description."
            )
        return (
            "Error: tool_call cannot call tool_call. Pass the target tool's "
            'own name, e.g. tool_call(name="artifact_register", arguments={...}).'
        )
