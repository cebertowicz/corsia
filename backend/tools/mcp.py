"""Canonical MCP sessions and native tools, shared by every harness."""

import asyncio
from contextlib import AsyncExitStack
from typing import Any

import httpx
from loguru import logger

from infra.db_async import run_in_db
from tools.base import Tool
from tools.mcp_config import (
    MCPConfigError, _active_auth, allowed_names, http_options, protect_transport_logs, public_name, safe_error,
    timeout_seconds, url_origin,
)
from tools.mcp_local_transport import (
    _mcp_denial_guidance,
    _mcp_denied_paths_in,
    mcp_oserror_detail as _mcp_oserror_detail,
    peer_cache_dirs,
    safehouse_stdio_parameters as _safehouse_stdio_parameters,
)
from tools.registry import ToolRegistry, _is_reserved_execution_param


class _StoredHeadersAuth(httpx.Auth):
    """Send managed header keys while keeping their values out of transport logs."""

    def __init__(
        self, headers: dict[str, str], origin: str | None = None, schemes: dict[str, str] | None = None,
    ) -> None:
        schemes = schemes or {}
        # Each stored value is the token alone; its scheme goes back in front when sent.
        self._headers = {name: f"{schemes[name]} {value}" if name in schemes else value for name, value in headers.items()}
        # The sent value and the bare token are both secret; the longer one is replaced first.
        self._secrets = sorted({*self._headers.values(), *headers.values()}, key=len, reverse=True)
        self._origin = url_origin(origin) if origin is not None else None

    def auth_flow(self, request):
        request_origin = url_origin(str(request.url))
        if self._origin is None:
            self._origin = request_origin
        if request_origin != self._origin:
            raise MCPConfigError("MCP endpoint changed origin; stored app keys were not sent")
        request.headers.update(self._headers)
        yield request

    def redact(self, value):
        if isinstance(value, str):
            for secret in self._secrets:
                if secret:
                    value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, dict):
            return {self.redact(key): self.redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        return value


class MCPConnectionError(MCPConfigError):
    """A safe connection error retaining the fixed runtime failure class."""

    def __init__(self, message: str, failure_class: str) -> None:
        super().__init__(message)
        self.failure_class = failure_class


class MCPConnectionFailure(str):
    """Legacy-compatible error text with structured safe classification."""

    failure_class: str

    def __new__(cls, message: str, failure_class: str) -> "MCPConnectionFailure":
        instance = super().__new__(cls, message)
        instance.failure_class = failure_class
        return instance


def mcp_failure_class(error: BaseException, config: dict) -> str:
    """Classify one launch failure before its safe text loses the machine cause."""
    if isinstance(error, BaseExceptionGroup):
        return mcp_failure_class(error.exceptions[0], config)
    if isinstance(error, MCPConnectionError):
        return error.failure_class
    if getattr(_active_auth.get(), "challenged", False):
        return "needs_sign_in"  # the server asked for an OAuth sign-in this connection did not have
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return "timed_out"
    if isinstance(error, OSError) and isinstance(config.get("command"), str):
        return "cannot_start"
    if isinstance(error, httpx.HTTPStatusError):
        if error.response.status_code in {401, 403} and (
            config.get("secret_env") or config.get("secret_headers")
        ):
            return "token_rejected"
        return "protocol_error"
    if isinstance(error, MCPConfigError) and "public tool name collision" in str(error):
        return "name_collision"
    return "protocol_error"


class MCPToolWrapper(Tool):
    """Wrap one upstream tool without leaking backend identity or credentials."""

    user_configured = True
    self_describing_errors = True

    def __init__(self, session, server_name: str, tool_def, tool_timeout: int = 30,
                 *, config: dict | None = None, auth=None, instructions: str | None = None,
                 app_id: str | None = None):
        config = config or {}
        if app_id is not None and (not isinstance(app_id, str) or not app_id):
            raise MCPConfigError("connected app identity must be a nonempty string")
        self._session = session
        self.mcp_server_name = server_name
        self.mcp_app_id = app_id
        self.mcp_server_config = config
        # The server's own description of itself, from its initialize result.
        self.mcp_instructions = instructions
        self.mcp_tool_name = tool_def.name
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
        if not getattr(result, "isError", False):
            return output
        if guidance := _mcp_denial_guidance(output):
            return f"Error: {guidance}"
        return f"Error: {output}"

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


async def _open_session(name: str, config: dict, stack: AsyncExitStack, peer_cache_dirs: tuple = ()):
    from mcp import ClientSession, StdioServerParameters

    from tools.mcp_oauth_auth import OAuthGrantAuth
    from tools.mcp_secrets import header_schemes, resolve

    auth = None
    transport_task: asyncio.Task | None = None
    secret_env, secret_headers = await run_in_db(resolve, config.get("connected_app_id", name), config)
    if config.get("command"):
        if config.get("bearer_file"):
            raise MCPConfigError("bearer_file is supported only for HTTP MCP servers")
        from mcp.client.stdio import stdio_client

        env = {**(config.get("env") or {}), **secret_env}
        params, profile_path = await asyncio.to_thread(
            _safehouse_stdio_parameters, name, config, config["command"], config.get("args", []), env, peer_cache_dirs,
        )
        stack.callback(profile_path.unlink, missing_ok=True)
        try:
            read, write = await stack.enter_async_context(stdio_client(params))
        except OSError as error:
            raise MCPConfigError(_mcp_oserror_detail(error)) from None
    elif config.get("url"):
        from mcp.client.sse import sse_client
        from tools.mcp_http_transport import sse_http_client_factory

        options, auth = http_options(config)
        if secret_headers:
            if auth is not None:
                raise MCPConfigError("stored header keys cannot be combined with bearer_file")
            auth = _StoredHeadersAuth(secret_headers, config["url"], header_schemes(config))
            options["auth"] = auth
        elif auth is None:
            auth = options["auth"] = OAuthGrantAuth(config.get("connected_app_id", name), config["url"])
        protect_transport_logs(auth)
        transport = config.get("transport", "http")
        if transport == "sse":
            read, write = await stack.enter_async_context(
                sse_client(
                    config["url"], headers=options["headers"], auth=auth,
                    httpx_client_factory=sse_http_client_factory,
                ),
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            initialized = await session.initialize()
        elif transport == "http":
            from tools.mcp_http_transport import open_streamable_http_session

            session, initialized, transport_task = await open_streamable_http_session(
                config["url"], options, auth, stack,
            )
        else:
            raise MCPConfigError("MCP transport is not supported")
    else:
        raise MCPConfigError("configure an MCP command or URL")
    if not config.get("url"):
        session = await stack.enter_async_context(ClientSession(read, write))
        initialized = await session.initialize()
    instructions = getattr(initialized, "instructions", None)
    return session, auth, instructions if isinstance(instructions, str) else None, transport_task


async def _discover_tools(name: str, config: dict, stack: AsyncExitStack, peer_cache_dirs: tuple = ()):
    allowlist = allowed_names(config)
    tool_timeout = timeout_seconds(config, "tool_timeout", 30)
    session, auth, instructions, transport_task = await _open_session(name, config, stack, peer_cache_dirs)
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
    app_id = config.get("connected_app_id")
    if app_id is not None and (not isinstance(app_id, str) or not app_id):
        raise MCPConfigError("connected app identity must be a nonempty string")
    return ([MCPToolWrapper(session, name, tool, tool_timeout, config=config, auth=auth,
                            instructions=instructions, app_id=app_id)
             for tool in discovered if allowlist is None or tool.name in allowlist], transport_task)


class _ServerConnection:
    """Enter and exit SDK task groups in one task, isolated from its callers."""

    def __init__(self, name: str, config: dict, peer_cache_dirs: tuple = ()):
        self.name, self.config, self.peer_cache_dirs = name, config, peer_cache_dirs
        self.ready = asyncio.get_running_loop().create_future()
        self.stop = asyncio.Event()
        self.error = "connection closed; restart the backend to reconnect the MCP server"
        self.task = asyncio.create_task(self._run(), name=f"mcp:{name}")

    async def _run(self):
        try:
            async with AsyncExitStack() as stack:
                wrappers, transport_task = await _discover_tools(self.name, self.config, stack, self.peer_cache_dirs)
                for wrapper in wrappers:
                    wrapper._connection = self
                self.ready.set_result(wrappers)
                if transport_task is None:
                    await self.stop.wait()
                else:
                    stop_wait = asyncio.create_task(self.stop.wait())
                    done, _ = await asyncio.wait(
                        {stop_wait, transport_task}, return_when=asyncio.FIRST_COMPLETED,
                    )
                    if stop_wait in done:
                        return
                    stop_wait.cancel()
                    await asyncio.gather(stop_wait, return_exceptions=True)
                    transport_task.result()
                    raise MCPConfigError("streamable HTTP connection closed")
        except asyncio.CancelledError as error:
            if self.stop.is_set():
                raise
            self.error = safe_error(error)
            if not self.ready.done():
                self.ready.set_exception(
                    MCPConnectionError(self.error, mcp_failure_class(error, self.config))
                )
        except (Exception, BaseExceptionGroup) as error:
            self.error = safe_error(error)
            if not self.ready.done():
                self.ready.set_exception(
                    MCPConnectionError(self.error, mcp_failure_class(error, self.config))
                )
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
    errors: dict[str, str] = {}
    registry.mcp_server_configs = {**getattr(registry, "mcp_server_configs", {}), **mcp_servers}
    from infra.config import STATE_ROOT, WORKSPACE
    from tools.catalog import TOOL_CATALOG

    for name, config in mcp_servers.items():
        connection = None
        try:
            deadline = timeout_seconds(config, "connect_timeout", 10)
            connection = _ServerConnection(
                name, config, peer_cache_dirs(name, mcp_servers, WORKSPACE, STATE_ROOT),
            )
            wrappers = await asyncio.wait_for(asyncio.shield(connection.ready), deadline)
            names = [tool.name for tool in wrappers]
            if len(names) != len(set(names)) or any(registry.has(tool) or tool in TOOL_CATALOG for tool in names):
                raise MCPConfigError("public tool name collision; configure distinct tool_prefix or public_names")
            for wrapper in wrappers:
                registry.register(wrapper)
            stack.push_async_callback(connection.close)
            logger.info("MCP server '{}': connected, {} tools registered", name, len(wrappers))
        except Exception as error:
            failure = MCPConnectionFailure(safe_error(error), mcp_failure_class(error, config))
            errors[name] = failure
            registry.mcp_server_states = {
                **getattr(registry, "mcp_server_states", {}), name: failure,
            }
            logger.error("MCP server '{}': {}", name, errors[name])
            if connection:
                await connection.close()
        except BaseException:
            if connection:
                await connection.close()
            raise
    return errors
