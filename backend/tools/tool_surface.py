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
from typing import TYPE_CHECKING, Any

from tools.base import Tool
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


def pack_index(registry: ToolRegistry) -> str:
    """Render the always-on index: one line per non-core tool, by pack.

    Tools from the user's configured MCP servers follow under their own
    heading; they load by name, not by pack.
    """
    lines = []
    for pack, names in pack_members().items():
        if not names:
            continue
        lines.append(f"[{pack}] {TOOL_PACKS[pack]}")
        lines.extend(f"- {name}: {TOOL_CATALOG[name].summary}" for name in names)
    configured = _user_configured(registry)
    if configured:
        lines.append("[mcp] Tools from your configured MCP servers (load by name)")
        for tool in configured:
            summary = (tool.description.strip().splitlines() or [tool.name])[0]
            lines.append(f"- {tool.name}: {summary[:_MCP_SUMMARY_CHARS]}")
    return "\n".join(lines)


def _user_configured(registry: ToolRegistry) -> list[Tool]:
    tools = (registry.get(name) for name in sorted(registry.tool_names))
    return [tool for tool in tools if tool is not None and tool.user_configured]


def _labelled(name: str) -> str:
    pack = tool_pack(name)
    return f"{name} (pack {pack})" if pack else name


def reachable_tool_names(registry: ToolRegistry) -> list[str]:
    """Return the registered tools ``tools_load`` and ``tool_call`` may reach."""
    configured = {tool.name for tool in _user_configured(registry)}
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
        self, pack: str | None = None, names: list[str] | None = None, **_: Any,
    ) -> str:
        """Return the requested schemas and how to call them."""
        requested = list(pack_members().get(pack, ())) if pack else []
        for name in names or ():
            canonical = normalize_tool_name(name)
            if canonical not in requested:
                requested.append(canonical)
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
