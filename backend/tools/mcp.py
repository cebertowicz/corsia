"""Canonical MCP sessions and native tools, shared by every harness."""

import asyncio
from contextlib import AsyncExitStack
from typing import Any

import httpx
from loguru import logger

from tools.base import Tool
from tools.mcp_config import (
    MCPConfigError, allowed_names, http_options, protect_transport_logs, public_name, safe_error,
    timeout_seconds,
)
from tools.registry import ToolRegistry, _is_reserved_execution_param


class MCPToolWrapper(Tool):
    """Wrap one upstream tool without leaking backend identity or credentials."""

    user_configured = True
    self_describing_errors = True

    def __init__(self, session, server_name: str, tool_def, tool_timeout: int = 30,
                 *, config: dict | None = None, auth=None):
        config = config or {}
        self._session = session
        self._original_name = tool_def.name
        self._name = public_name(server_name, tool_def.name, config)
        self._auth = auth
        self._connection = None
        if self._redact(self._name) != self._name:
            raise MCPConfigError("server advertised a credential in a tool name")
        self._description = self._redact(tool_def.description or tool_def.name)
        self._parameters = self._redact(tool_def.inputSchema or {"type": "object", "properties": {}})
        self._tool_timeout = tool_timeout
        self.always_advertised = config.get("always_load") is True
        self._allowed_user_ids = config.get("allowed_user_ids")
        if self._allowed_user_ids is not None and (
            not isinstance(self._allowed_user_ids, list)
            or any(not isinstance(value, str) or not value for value in self._allowed_user_ids)
        ):
            raise MCPConfigError("allowed_user_ids must be a list of authorized CORSIA user IDs")

    def _redact(self, value):
        return self._auth.redact(value) if self._auth else value

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    async def execute(self, **kwargs: Any) -> str:
        from mcp import types

        if not await self._authorized_user():
            return "Error: This MCP server is not authorized for this conversation's user."
        arguments = {key: value for key, value in kwargs.items()
                     if not _is_reserved_execution_param(key)}
        try:
            result = await self._call(arguments)
        except Exception as error:
            return f"Error: MCP tool '{self._name}': {safe_error(error)}."
        parts = [block.text if isinstance(block, types.TextContent) else str(block)
                 for block in result.content]
        output = self._redact("\n".join(parts) or "(no output)")
        return f"Error: {output}" if getattr(result, "isError", False) else output

    async def _authorized_user(self):
        if self._allowed_user_ids is None:
            return True
        from infra import state
        from infra.db_async import run_in_db
        from infra.queries import load_thread_row

        thread_id = state._current_thread_id.get(None)
        if not thread_id:
            return False
        thread = await run_in_db(load_thread_row, thread_id)
        return thread.get("user_id") in self._allowed_user_ids

    async def _call(self, arguments):
        call = asyncio.create_task(self._session.call_tool(self._original_name, arguments=arguments))
        pending = [call]
        if self._connection is not None:
            pending.append(self._connection.task)
        try:
            done, _ = await asyncio.wait(pending, timeout=self._tool_timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
            if call in done:
                return call.result()
            if self._connection is not None and self._connection.task in done:
                raise MCPConfigError(self._connection.error)
            raise TimeoutError
        finally:
            if not call.done():
                call.cancel()
            await asyncio.gather(call, return_exceptions=True)


async def _open_session(name: str, config: dict, stack: AsyncExitStack):
    from mcp import ClientSession, StdioServerParameters

    from tools.mcp_secrets import resolve

    auth = None
    if config.get("secret_headers"):
        # A legacy credential header stays refused at launch, before any value is read.
        raise MCPConfigError("literal credential headers are refused; use bearer_file for bearer authentication")
    secret_env, _ = resolve(name, config)
    if config.get("command"):
        if config.get("bearer_file"):
            raise MCPConfigError("bearer_file is supported only for HTTP MCP servers")
        from mcp.client.stdio import stdio_client

        env = {**(config.get("env") or {}), **secret_env}
        params = StdioServerParameters(command=config["command"],
                                       args=config.get("args", []), env=env or None)
        read, write = await stack.enter_async_context(stdio_client(params))
    elif config.get("url"):
        from mcp.client.streamable_http import streamable_http_client

        options, auth = http_options(config)
        protect_transport_logs(auth)
        client = await stack.enter_async_context(httpx.AsyncClient(**options))
        read, write, _ = await stack.enter_async_context(
            streamable_http_client(config["url"], http_client=client),
        )
    else:
        raise MCPConfigError("configure an MCP command or URL")
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return session, auth


async def _discover_tools(name: str, config: dict, stack: AsyncExitStack):
    allowlist = allowed_names(config)
    tool_timeout = timeout_seconds(config, "tool_timeout", 30)
    session, auth = await _open_session(name, config, stack)
    discovered = []
    cursor = None
    while True:
        page = await session.list_tools(cursor=cursor)
        discovered.extend(page.tools)
        if not page.nextCursor:
            break
        if page.nextCursor == cursor:
            raise MCPConfigError("MCP tools/list returned a repeated page cursor")
        cursor = page.nextCursor
    if allowlist is not None and not allowlist.issubset({tool.name for tool in discovered}):
        raise MCPConfigError("server did not advertise every allowed tool; check the pinned allowlist")
    return [MCPToolWrapper(session, name, tool, tool_timeout, config=config, auth=auth)
            for tool in discovered if allowlist is None or tool.name in allowlist]


class _ServerConnection:
    """Enter and exit SDK task groups in one task, isolated from its callers."""

    def __init__(self, name: str, config: dict):
        self.name, self.config = name, config
        self.ready = asyncio.get_running_loop().create_future()
        self.stop = asyncio.Event()
        self.error = "connection closed; restart the backend to reconnect the MCP server"
        self.task = asyncio.create_task(self._run(), name=f"mcp:{name}")

    async def _run(self):
        try:
            async with AsyncExitStack() as stack:
                wrappers = await _discover_tools(self.name, self.config, stack)
                for wrapper in wrappers:
                    wrapper._connection = self
                self.ready.set_result(wrappers)
                await self.stop.wait()
        except Exception as error:
            self.error = safe_error(error)
            if not self.ready.done():
                self.ready.set_exception(MCPConfigError(safe_error(error)))
            else:
                logger.error("MCP server '{}': {}", self.name, safe_error(error))
        finally:
            if not self.ready.done():
                self.ready.cancel()

    async def close(self):
        """Stop transport contexts in their owning task, including failed startup."""
        self.stop.set()
        if not self.ready.done():
            self.task.cancel()
        from app_shutdown_join import join_cleanup_owner

        result, pending_cancel = await join_cleanup_owner(asyncio.wait_for(self.task, 10))
        if self.ready.done() and not self.ready.cancelled():
            self.ready.exception()
        if pending_cancel is not None:
            raise pending_cancel
        if isinstance(result, TimeoutError):
            raise RuntimeError("MCP connection shutdown timed out") from None


async def connect_mcp_servers(mcp_servers: dict, registry: ToolRegistry,
                              stack: AsyncExitStack) -> dict[str, str]:
    """Discover configured tools; the supplied stack owns bounded connection cleanup.

    SDK task groups stay in their server task, so 401, cancellation or a failed
    server cannot cancel the caller or another server. Publish each server's
    complete allowlisted set atomically, without replacing built-ins.
    """
    errors = {}
    from tools.catalog import TOOL_CATALOG

    for name, config in mcp_servers.items():
        connection = None
        try:
            deadline = timeout_seconds(config, "connect_timeout", 10)
            connection = _ServerConnection(name, config)
            wrappers = await asyncio.wait_for(asyncio.shield(connection.ready), deadline)
            names = [tool.name for tool in wrappers]
            if len(names) != len(set(names)) or any(registry.has(tool) or tool in TOOL_CATALOG for tool in names):
                raise MCPConfigError("public tool name collision; configure distinct tool_prefix or public_names")
            for wrapper in wrappers:
                registry.register(wrapper)
            stack.push_async_callback(connection.close)
            logger.info("MCP server '{}': connected, {} tools registered", name, len(wrappers))
        except Exception as error:
            errors[name] = safe_error(error)
            logger.error("MCP server '{}': {}", name, errors[name])
            if connection:
                await connection.close()
        except BaseException:
            if connection:
                await connection.close()
            raise
    return errors
