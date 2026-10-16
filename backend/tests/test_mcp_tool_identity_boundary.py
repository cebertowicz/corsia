from __future__ import annotations

from pathlib import Path
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


def test_mcp_denied_paths_include_shell_and_native_formats():
    from tools.mcp import _mcp_denied_paths_in

    paths = _mcp_denied_paths_in(
        "cat: /state/shell.db: Operation not permitted\n"
        "EPERM: operation not permitted, open '/state/native.db'"
    )

    assert paths == (Path("/state/shell.db"), Path("/state/native.db"))


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
@pytest.mark.parametrize("output", [
    "[Errno 1] Operation not permitted: '/private/state/corsia.db'",
    "EPERM: operation not permitted, open '/private/state/corsia.db'",
])
async def test_mcp_wrapper_preserves_successful_operation_not_permitted_text(monkeypatch, output):
    def unexpected_guidance(_paths):
        pytest.fail("successful MCP output must not be classified as a denial")

    monkeypatch.setattr(
        "tools.host_path_policy.current_protected_host_path_policy",
        lambda: SimpleNamespace(denial_guidance_for_paths=unexpected_guidance),
    )

    assert await _wrapper_for_output(output, is_error=False).execute() == output


@pytest.mark.asyncio
@pytest.mark.parametrize("output", [
    "[Errno 1] Operation not permitted: '/ordinary/report.txt'",
    '{"error": "EPERM: operation not permitted, open \'/ordinary/report.txt\'"}',
])
async def test_mcp_wrapper_preserves_nonprivate_eperm_error_text(monkeypatch, output):
    seen_paths = []

    def no_guidance(paths):
        seen_paths.extend(paths)
        return None

    monkeypatch.setattr(
        "tools.host_path_policy.current_protected_host_path_policy",
        lambda: SimpleNamespace(denial_guidance_for_paths=no_guidance),
    )

    assert await _wrapper_for_output(output, is_error=True).execute() == f"Error: {output}"
    assert seen_paths == [Path("/ordinary/report.txt")]


@pytest.mark.asyncio
async def test_mcp_wrapper_rewrites_private_path_denial(monkeypatch):
    monkeypatch.setattr(
        "tools.host_path_policy.current_protected_host_path_policy",
        lambda: SimpleNamespace(
            denial_guidance_for_paths=lambda _paths: "/state/corsia.db — requires reviewed host access",
        ),
    )

    output = '{"error": "[Errno 1] Operation not permitted: \'/state/corsia.db\'"}'
    assert await _wrapper_for_output(output, is_error=True).execute() == (
        "Error: /state/corsia.db — CORSIA private state is blocked for local MCP apps. "
        "Nothing was returned. Use the app's own working directory or a supported "
        "non-private integration."
    )
