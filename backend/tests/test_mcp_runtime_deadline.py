"""The local runtime download precedes the MCP connection deadline."""

import asyncio
import time
from contextlib import AsyncExitStack

import pytest

from tools import mcp, managed_runtime
from tools.registry import ToolRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize("download_fails", [False, True])
async def test_runtime_download_precedes_connect_deadline(monkeypatch, download_fails):
    calls = []

    def fetch_plan(command, *, path=None):
        assert command == "uvx"
        return "uv", object()

    def download(kind):
        calls.append(kind)
        time.sleep(0.08)
        if download_fails:
            raise managed_runtime.ManagedRuntimeDownloadError("download failed")

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
    if download_fails:
        assert errors["app"].failure_class == "runtime_download_failed"
    else:
        assert errors == {}
