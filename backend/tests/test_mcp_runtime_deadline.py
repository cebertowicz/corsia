"""The local runtime download precedes the MCP connection deadline."""

import asyncio
import time
from contextlib import AsyncExitStack

import pytest

from tools import mcp, managed_runtime
from tools.registry import ToolRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize("failure, expected", [
    (None, None),
    (managed_runtime.ManagedRuntimeDownloadError("download failed"), "runtime_download_failed"),
    (managed_runtime.ManagedRuntimeError("download archive has an unsafe path"), "cannot_start"),
])
async def test_runtime_download_precedes_connect_deadline(monkeypatch, failure, expected):
    calls = []

    def fetch_plan(command, *, path=None):
        assert command == "uvx"
        return "uv", object()

    def download(kind):
        calls.append(kind)
        time.sleep(0.08)
        if failure is not None:
            raise failure

    async def discover(_name, _config, _stack):
        await asyncio.sleep(0)
        return [], None

    monkeypatch.setattr(managed_runtime, "runtime_fetch_plan", fetch_plan)
    monkeypatch.setattr(managed_runtime, "ensure_runtime", download)
    monkeypatch.setattr(mcp, "_discover_tools", discover)
    registry = ToolRegistry()
    async with AsyncExitStack() as stack:
        errors = await mcp.connect_mcp_servers(
            {"app": {"command": "uvx", "connect_timeout": 0.02}}, registry, stack,
        )
    assert calls == ["uv"]
    if expected is not None:
        assert errors["app"].failure_class == expected
    else:
        assert errors == {}
