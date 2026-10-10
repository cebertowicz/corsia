from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_mcp_wrapper_never_forwards_backend_execution_context():
    from tools.mcp import MCPToolWrapper

    session = SimpleNamespace(
        call_tool=AsyncMock(return_value=SimpleNamespace(content=[])),
    )
    wrapper = MCPToolWrapper(
        session,
        "external",
        SimpleNamespace(
            name="remote",
            description="remote tool",
            inputSchema={"type": "object", "properties": {}},
        ),
    )

    assert await wrapper.execute(
        public="value",
        _thread_id="private-thread",
        _thread_user_id="private-user",
        _thread_project_id="private-project",
        _host_access_lease_id="private-lease",
        _lease_permit="private-permit",
    ) == "(no output)"

    session.call_tool.assert_awaited_once_with(
        "remote",
        arguments={"public": "value"},
    )
