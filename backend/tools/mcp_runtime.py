"""Application-owned MCP discovery.

Discovery completes at startup, before any turn is prepared. A later import
only adds servers: reload connects the newly configured ones into the live
session set and leaves every existing session untouched.

Start, reload and stop serialize on one lock, and a stop is final until the
next explicit start: a reload that lands during or after shutdown opens nothing.
"""

import asyncio
from contextlib import AsyncExitStack

from infra import state
from tools.mcp import connect_mcp_servers
from tools.registry import ToolRegistry

_registry = ToolRegistry()
_stack: AsyncExitStack | None = None
_connected: set[str] = set()
_stopped = False
_lock = asyncio.Lock()


async def start_mcp_runtime(config: dict) -> dict[str, str]:
    """Create the one shared session set at startup, including an empty config."""
    global _stack, _stopped
    async with _lock:
        if _stack is not None:
            raise RuntimeError("MCP runtime already started")
        _stopped = False
        _stack = AsyncExitStack()
        try:
            return await _connect_new(config)
        except BaseException:
            await _close()
            raise


async def _connect_new(config: dict) -> dict[str, str]:
    """Connect configured servers that are not yet connected into the live set."""
    pending = {name: server for name, server in config.items() if name not in _connected}
    errors = await connect_mcp_servers(pending, _registry, _stack)
    _connected.update(name for name in pending if name not in errors)
    return errors


async def reload_mcp_runtime(config: dict) -> dict[str, str]:
    """Add newly configured servers without disturbing sessions already in use.

    Existing sessions stay open, so no in-flight call loses its session and the
    drain contract in ``stop_mcp_runtime`` is untouched. After a stop nothing is
    opened: each server not yet connected reports that the runtime is stopped.

    The import is a deliberate trade: the new tools reach the cached native agent
    at once, so a conversation that is already live sees them on its next turn
    (the onboarding conductor sets up an MCP connection and uses it in the same
    conversation). That turn sends a different tool block, so each live
    conversation loses its cached prompt prefix once. Nothing churns after
    that: the advertised tool list is sorted by name
    (``tools.catalog.advertised_tool_definitions``), so later turns match
    byte for byte and cache again.
    """
    global _stack
    async with _lock:
        if _stopped:
            return {name: "MCP runtime is stopped" for name in config if name not in _connected}
        if _stack is None:
            _stack = AsyncExitStack()
        errors = await _connect_new(config)
        if state._agent is not None:
            register_mcp_tools(state._agent.tools)
        return errors


def register_mcp_tools(registry: ToolRegistry) -> None:
    """Populate native and harness registries from the completed startup snapshot."""
    for name in _registry.tool_names:
        if registry.has(name) and registry.get(name) is not _registry.get(name):
            raise RuntimeError("Configured MCP tool collides with an existing tool")
        registry.register(_registry.get(name))


async def _close() -> None:
    """Close the live sessions and discard the snapshot; the caller holds the lock."""
    global _stack, _registry
    stack, _stack = _stack, None
    _connected.clear()
    try:
        if stack is not None:
            await stack.aclose()
    finally:
        _registry = ToolRegistry()


async def stop_mcp_runtime() -> None:
    """Close sessions after conversation owners drain, then discard the snapshot."""
    global _stopped
    async with _lock:
        _stopped = True
        await _close()
