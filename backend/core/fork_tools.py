"""One policy boundary for disposable harness tools, independent of foreground Turns."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from tools.catalog import normalize_tool_name


async def execute_fork_tool(kind, request, registry, cancel_event, name, arguments):
    """Apply the kind before dispatch; its executor owns mutation scope."""
    from infra import state

    if cancel_event.is_set():
        raise asyncio.CancelledError
    name = normalize_tool_name(name)
    if name not in kind.tool_allowlist:
        return kind.tool_refusal
    thread_token = state._current_thread_id.set(request.thread_id)
    turn_token = state._current_turn_identity.set(None)
    try:
        return await kind.tool_executor(
            SimpleNamespace(tools=registry), name, arguments, cancel_event, request,
        )
    finally:
        state._current_turn_identity.reset(turn_token)
        state._current_thread_id.reset(thread_token)


class ForkToolBoundary:
    """Kind policy and accounting attached to a disposable Codex session."""

    def __init__(self, kind, request, registry, cancel_event, fits):
        self.kind, self.request, self.registry = kind, request, registry
        self.cancel_event, self.fits = cancel_event, fits

    async def __call__(self, reply, usage=None):
        if self.cancel_event.is_set():
            raise asyncio.CancelledError
        if self.fits is None:
            return True
        from inspect import isawaitable

        result = self.fits(reply) if usage is None else self.fits(reply, usage)
        return await result if isawaitable(result) else result

    async def execute(self, name, arguments):
        return await execute_fork_tool(
            self.kind, self.request, self.registry, self.cancel_event, name, arguments,
        )


def codex_fork_tools(request, cwd: str, kind, capabilities: frozenset[str]):
    """Use the exact cold surface frozen at fit, or rebuild warm callbacks."""
    from subagents.harness_tool_bridge import build_tool_registry, dynamic_tool_definitions
    from tools.catalog import fork_tool_definitions

    if request.warm_source is not None:
        # Preserve the source's cached prefix; execution still enforces the allowlist.
        registry = request.tool_registry or build_tool_registry(cwd)
        return registry, dynamic_tool_definitions(registry, capabilities)
    if request.tool_registry is not None:
        definitions = (
            fork_tool_definitions(request.tool_registry, kind.tool_allowlist)
            if hasattr(request.tool_registry, "get_definitions") else request.dynamic_tool_specs
        )
        return request.tool_registry, allowed_definitions(definitions, kind)
    registry = build_tool_registry(cwd)
    definitions = allowed_definitions(
        fork_tool_definitions(registry, kind.tool_allowlist)
        if hasattr(registry, "get_definitions") else dynamic_tool_definitions(registry, capabilities),
        kind,
    )
    return registry, definitions


def allowed_definitions(definitions, kind):
    """Handle both OpenAI function definitions and Codex dynamic definitions."""
    selected = tuple(
        item for item in definitions
        if (item.get("function") or item).get("name") in kind.tool_allowlist
    )
    return definitions if isinstance(definitions, tuple) and len(selected) == len(definitions) else selected
