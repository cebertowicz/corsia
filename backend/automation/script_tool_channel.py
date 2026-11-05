"""Run-scoped tool calls as the owner of an authenticated script."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from infra import state
from infra.tool_result_errors import is_error_result
from subagents.harness_tool_bridge import build_tool_registry
from tools.registry import ToolRegistry

SOCKET_ENV = "CORSIA_SCRIPT_TOOL_SOCKET"


async def _serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *,
                 thread_id: str, registry: ToolRegistry) -> None:
    try:
        request: Any = json.loads(await reader.read())
        if not isinstance(request, dict) or not isinstance(request.get("name"), str) or not isinstance(request.get("arguments"), dict):
            raise ValueError("expected a tool name and JSON object")
        context = state._current_thread_id.set(thread_id)
        turn = state._current_turn_identity.set(None)
        try:
            with state.tool_invocation_context(None):
                result = await registry.execute(request["name"], request["arguments"])
        finally:
            state._current_turn_identity.reset(turn)
            state._current_thread_id.reset(context)
        response = {"code": 1 if is_error_result(result) else 0, "stdout": str(result), "stderr": ""}
    except Exception:
        response = {"code": 1, "stdout": "", "stderr": "Tool request failed.\n"}
    try:
        data = json.dumps(response).encode()
        writer.write(len(data).to_bytes(8, "big") + data)
        await writer.drain()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


@asynccontextmanager
async def tool_channel(path: Path, thread_id: str, *, registry: ToolRegistry | None = None):
    """Serve tool calls for the lifetime of one script run."""
    registry = registry or await asyncio.to_thread(build_tool_registry)
    active: set[asyncio.Task] = set()

    def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(_serve(reader, writer, thread_id=thread_id, registry=registry))
        active.add(task)
        task.add_done_callback(active.discard)

    server = await asyncio.start_unix_server(connected, path=str(path))
    try:
        path.chmod(0o600)
        yield
    finally:
        server.close()
        await server.wait_closed()
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        path.unlink(missing_ok=True)
