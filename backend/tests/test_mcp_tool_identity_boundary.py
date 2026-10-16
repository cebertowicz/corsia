from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _wrapper_for_output(output: str, *, is_error: bool):
    from tools.mcp import MCPToolWrapper

    session = SimpleNamespace(
        call_tool=AsyncMock(return_value=SimpleNamespace(content=[output], isError=is_error)),
    )
    return MCPToolWrapper(
        session,
        "external",
        SimpleNamespace(
            name="remote",
            description="remote tool",
            inputSchema={"type": "object", "properties": {}},
        ),
    )


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


@pytest.mark.asyncio
async def test_mcp_wrapper_preserves_successful_operation_not_permitted_text(monkeypatch):
    def unexpected_guidance(_text: str):
        pytest.fail("successful MCP output must not be classified as a denial")

    monkeypatch.setattr("tools.host_path_policy.denial_guidance_for_output", unexpected_guidance)
    output = "Issue body: Operation not permitted can be expected here"

    assert await _wrapper_for_output(output, is_error=False).execute() == output


@pytest.mark.asyncio
async def test_mcp_wrapper_preserves_nonprivate_eperm_error_text(monkeypatch):
    monkeypatch.setattr(
        "tools.host_path_policy.denial_guidance_for_output",
        lambda _text: (
            "/Users/example/Desktop/report — SSH private material is blocked in protected mode. "
            "No host-access lease can open it."
        ),
    )
    output = "/Users/example/Desktop/report: Operation not permitted"

    assert await _wrapper_for_output(output, is_error=True).execute() == f"Error: {output}"


@pytest.mark.asyncio
async def test_mcp_wrapper_rewrites_private_path_denial(monkeypatch):
    monkeypatch.setattr(
        "tools.host_path_policy.denial_guidance_for_output",
        lambda _text: "/state/corsia.db — requires reviewed host access",
    )

    assert await _wrapper_for_output("/state/corsia.db: Operation not permitted", is_error=True).execute() == (
        "Error: /state/corsia.db — CORSIA private state is blocked for local MCP apps. "
        "Nothing was returned. Use the app's own working directory or a supported "
        "non-private integration."
    )
