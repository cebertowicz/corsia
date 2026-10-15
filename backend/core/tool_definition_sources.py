"""Source-inventory rows for the tool list: one per connected app, plus CORSIA's own.

The old single "Tool definitions" row held the whole advertised list. The
``[mcp]`` index lines inside ``tools_load``'s description are what each
connected app costs on every turn, so each gets its own row, and the rest of
the list stays one row that absorbs the remainder. The rows add up to the
measured total by construction, so the sheet reconciles to its headline.
"""

from __future__ import annotations

import re
from typing import Any

from core.effective_context import ContextSourceEntry
from core.token_estimation import _text_tokens
from tools.tool_surface import app_display_name

_INDEX_LINE = re.compile(r"^- (?P<name>[^:]+): (?P<count>\d+) tools?; .+$", re.MULTILINE)


def mcp_index_lines(definitions: Any) -> list[tuple[str, int, str]]:
    """The frozen per-app index lines in ``tools_load``'s description."""
    for definition in definitions:
        function = definition.get("function", {}) if isinstance(definition, dict) else {}
        if function.get("name") != "tools_load":
            continue
        description = function.get("description", "")
        if not isinstance(description, str) or "[mcp] Tool servers." not in description:
            return []
        block = description.split("[mcp] Tool servers.", 1)[1]
        return [
            (match.group("name"), int(match.group("count")), match.group(0))
            for match in _INDEX_LINE.finditer(block)
        ]
    return []


def tool_definition_entries(
    *,
    id_prefix: str,
    thread_id: str,
    freshness: str,
    model: str | None,
    canonical_tool_definitions: str,
    frozen_tool_definitions: Any,
    total_tokens: int,
    single_label: str,
) -> list[ContextSourceEntry]:
    """Per-app index rows and the CORSIA remainder; ``total_tokens`` is the old single row."""
    lines = mcp_index_lines(frozen_tool_definitions)
    reference = f"thread://{thread_id}/tool-definitions"
    tokens = [_text_tokens(line, model) for _name, _count, line in lines]
    entries = [
        ContextSourceEntry(
            source_id=f"{id_prefix}:tool-index:{name}", origin_section="tools", label=name,
            stable_reference=f"{reference}/{name}", freshness=freshness, inclusion="included",
            reason="Always-on connected-app index line", owner_surface="tool-registry",
            prompt_content=line, prompt_tokens_override=cost,
            metadata={
                "provider": "mcp_index", "server_name": name,
                "display_name": app_display_name(name), "tool_count": count,
            },
        )
        for (name, count, line), cost in zip(lines, tokens, strict=True)
    ]
    entries.append(ContextSourceEntry(
        source_id=f"{id_prefix}:tool-definitions", origin_section="tools",
        label="CORSIA tools" if lines else single_label, stable_reference=reference,
        freshness=freshness, inclusion="included",
        reason=(
            "CORSIA tool definitions after the connected-app index lines" if lines
            else "Exact selected tool schema ledger for this harness"
        ),
        owner_surface="tool-registry", prompt_content=canonical_tool_definitions,
        prompt_tokens_override=max(0, total_tokens - sum(tokens)) if lines else total_tokens,
        metadata={"tool_count": len(frozen_tool_definitions), **({"provider": "corsia_tools"} if lines else {})},
    ))
    return entries
