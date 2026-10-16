"""Canonical streamable HTTP initialization and legacy SSE fallback for MCP."""

import asyncio
from contextlib import AsyncExitStack
from typing import Any

import httpx


async def _own_streamable_session(url: str, client: httpx.AsyncClient, ready, closed: asyncio.Event) -> None:
    """Own the SDK task group in one task so it closes in the task that entered it."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    try:
        async with AsyncExitStack() as stream_stack:
            read, write, _ = await stream_stack.enter_async_context(
                streamable_http_client(url, http_client=client),
            )
            session = await stream_stack.enter_async_context(ClientSession(read, write))
            initialized = await session.initialize()
            ready.set_result((session, initialized))
            await closed.wait()
    except BaseException as error:
        if not ready.done():
            ready.set_exception(error)
        else:
            raise


async def _open_legacy_sse(url: str, headers: dict, auth: httpx.Auth | None, stack: AsyncExitStack):
    """Open the SDK SSE transport for a declared or detected legacy endpoint."""
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    read, write = await stack.enter_async_context(sse_client(url, headers=headers, auth=auth))
    session = await stack.enter_async_context(ClientSession(read, write))
    return session, await session.initialize()


async def open_streamable_http_session(
    url: str, options: dict[str, Any], auth: httpx.Auth | None, stack: AsyncExitStack,
):
    """Initialize streamable HTTP, falling back to SSE only on initialize 404/405."""
    statuses: set[int] = set()

    async def record_status(response: httpx.Response) -> None:
        if response.request.method == "POST":
            statuses.add(response.status_code)

    client = await stack.enter_async_context(httpx.AsyncClient(
        **options, event_hooks={"response": [record_status]},
    ))
    ready = asyncio.get_running_loop().create_future()
    closed = asyncio.Event()
    task = asyncio.create_task(_own_streamable_session(url, client, ready, closed))

    async def close() -> None:
        closed.set()
        await asyncio.gather(task, return_exceptions=True)

    try:
        session, initialized = await asyncio.shield(ready)
    except asyncio.CancelledError:
        await close()
        if not (task.done() and statuses.intersection({404, 405})):
            raise
        session, initialized = await _open_legacy_sse(url, options["headers"], auth, stack)
    except (Exception, BaseExceptionGroup):
        await close()
        # The SDK exposes 404 as JSON-RPC termination and can wrap a 405 in a
        # task group, so the response hook is the reliable fallback signal.
        if not statuses.intersection({404, 405}):
            raise
        session, initialized = await _open_legacy_sse(url, options["headers"], auth, stack)
    else:
        stack.push_async_callback(close)
        return session, initialized, task
    return session, initialized, None
