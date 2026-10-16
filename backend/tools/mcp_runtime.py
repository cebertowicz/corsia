"""Application-owned MCP discovery.

Discovery completes at startup, before any turn is prepared. A later import
only adds servers: reload connects the newly configured ones into the live
session set and leaves every existing session untouched.

Start, reload and stop serialize on one lock, and a stop is final until the
next explicit start: a reload that lands during or after shutdown opens nothing.
"""

import asyncio
import time
from contextlib import AsyncExitStack

from infra import state
from infra.db_async import run_in_db
from infra.time import utc_now_iso
from tools.mcp import connect_mcp_servers
from tools.registry import ToolRegistry

_registry = ToolRegistry()
_stack: AsyncExitStack | None = None
_connected: set[str] = set()
_stopped = False
_lock = asyncio.Lock()
_app_stacks: dict[str, AsyncExitStack] = {}
_app_tools: dict[str, set[str]] = {}
_app_servers: dict[str, str] = {}
_app_states: dict[str, dict[str, object]] = {}
_reconnect_at: dict[str, float] = {}


class MCPReconnectRateLimitError(RuntimeError):
    """A connected app was already restarted within its ten-second window."""


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


async def _publish_app_state(app_id: str, result: dict[str, object]) -> None:
    """Store and passively publish a changed app runtime state once."""
    previous = _app_states.get(app_id)
    _app_states[app_id] = result
    if previous is not None and (
        previous.get("state"), previous.get("failure_class"), previous.get("tool_count"),
    ) == (
        result.get("state"), result.get("failure_class"), result.get("tool_count"),
    ):
        return
    from connections.app_reach import record_runtime_app_state

    await run_in_db(record_runtime_app_state, app_id, result)


async def _remove_connected_app(app_id: str) -> None:
    """Withdraw one app's wrappers and session without disturbing other apps."""
    names = _app_tools.pop(app_id, set())
    for name in names:
        _registry.unregister(name)
        if state._agent is not None:
            state._agent.tools.unregister(name)
    server = _app_servers.pop(app_id, None)
    if server is not None:
        getattr(_registry, "mcp_server_configs", {}).pop(server, None)
        getattr(_registry, "mcp_server_states", {}).pop(server, None)
    stack = _app_stacks.pop(app_id, None)
    if stack is not None:
        await stack.aclose()


async def apply_connected_app_runtime(action: str, app_id: str) -> dict[str, object]:
    """Connect, reconnect or remove exactly one stable-id connected app."""
    async with _lock:
        if action == "remove":
            await _remove_connected_app(app_id)
            _app_states.pop(app_id, None)
            _reconnect_at.pop(app_id, None)
            return {"state": "disconnected", "state_since": utc_now_iso(), "tool_count": 0, "failure_class": None}
        if _stopped:
            return {"state": "failed", "state_since": utc_now_iso(), "tool_count": 0, "failure_class": "runtime_stopped"}
        if action == "reconnect":
            now = time.monotonic()
            if now - _reconnect_at.get(app_id, float("-inf")) < 10:
                raise MCPReconnectRateLimitError("This app was just reconnected; try again in a few seconds.")
            _reconnect_at[app_id] = now
            await _remove_connected_app(app_id)
        elif app_id in _app_stacks:
            return _app_states[app_id]
        from connections.app_reach import load_runtime_app_config

        name, config = await run_in_db(load_runtime_app_config, app_id)
        _app_servers[app_id] = name
        stack = AsyncExitStack()
        errors = await connect_mcp_servers({name: config}, _registry, stack)
        if error := errors.get(name):
            await stack.aclose()
            _app_servers.pop(app_id, None)
            result = {"state": "failed", "state_since": utc_now_iso(), "tool_count": 0, "failure_class": "connection_failed"}
        else:
            names = {
                name for name in _registry.tool_names
                if getattr(_registry.get(name), "mcp_app_id", None) == app_id
            }
            _app_stacks[app_id] = stack
            _app_tools[app_id] = names
            result = {"state": "working", "state_since": utc_now_iso(), "tool_count": len(names), "failure_class": None}
        await _publish_app_state(app_id, result)
        if state._agent is not None:
            register_mcp_tools(state._agent.tools)
        return result


async def start_connected_app_runtime() -> dict[str, dict[str, object]]:
    """Start every approved durable app after the base MCP runtime starts."""
    from connections.app_reach import load_runtime_app_ids

    results = {}
    for app_id in await run_in_db(load_runtime_app_ids):
        results[app_id] = await apply_connected_app_runtime("connect", app_id)
    return results


def connected_app_state(app_id: str) -> dict[str, object]:
    """Return bounded live state for the Connected Apps management route."""
    return _app_states.get(app_id, {"state": "disconnected", "state_since": None, "tool_count": 0, "failure_class": None})


def register_mcp_tools(registry: ToolRegistry) -> None:
    """Populate native and harness registries from the completed startup snapshot."""
    registry.mcp_server_configs = getattr(_registry, "mcp_server_configs", {})
    registry.mcp_server_states = getattr(_registry, "mcp_server_states", {})
    for name in _registry.tool_names:
        if registry.has(name) and registry.get(name) is not _registry.get(name):
            raise RuntimeError("Configured MCP tool collides with an existing tool")
        registry.register(_registry.get(name))


def tool_server_index() -> dict[str, str]:
    """Map each live MCP tool's public name to the server it belongs to."""
    index = {}
    for name in _registry.tool_names:
        server = getattr(_registry.get(name), "mcp_server_name", None)
        if isinstance(server, str) and server:
            index[name] = server
    return index


async def _close() -> None:
    """Close every live session and discard the runtime snapshots."""
    global _stack, _registry
    stack, _stack = _stack, None
    _connected.clear()
    app_stacks = tuple(_app_stacks.values())
    _app_stacks.clear()
    _app_tools.clear()
    _app_servers.clear()
    _app_states.clear()
    _reconnect_at.clear()
    try:
        for app_stack in app_stacks:
            await app_stack.aclose()
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
