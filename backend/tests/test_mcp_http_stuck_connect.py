"""A server that never answers initialize must not keep a cancelled connect alive."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

import httpx
import pytest

from tools.mcp_http_transport import open_streamable_http_session


async def test_cancelling_a_connect_to_a_silent_server_returns_at_once():
    never = asyncio.Event()

    async def silent(request: httpx.Request) -> httpx.Response:
        await never.wait()
        return httpx.Response(200)

    options = {"headers": {}, "transport": httpx.MockTransport(silent)}
    async with AsyncExitStack() as stack:
        connect = asyncio.create_task(open_streamable_http_session("http://127.0.0.1:9/mcp", options, None, stack))
        await asyncio.sleep(0.2)  # initialize is now waiting on the server
        connect.cancel()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(connect, 2)
