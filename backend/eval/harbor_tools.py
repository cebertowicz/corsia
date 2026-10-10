"""Disposable-only container tools backed by the Harbor Unix-socket bridge."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from tools.base import Tool


_SOCKET_ENV = "CORSIA_HARBOR_BRIDGE_SOCKET"


class HarborBridgeTool(Tool):
    """Forward one named container operation to the task-scoped Harbor bridge."""

    def __init__(self, operation: str) -> None:
        self._operation = operation

    @property
    def name(self) -> str:
        return f"harbor_{self._operation}"

    @property
    def description(self) -> str:
        return f"Run {self._operation} only inside the Harbor task container."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string"},
                "timeout": {"type": "number", "minimum": 0.1},
                "path": {"type": "string"},
                "content": {"type": "string"},
                "local_name": {"type": "string"},
            },
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        socket_path = os.environ.get(_SOCKET_ENV)
        if not socket_path:
            return f"Error: {_SOCKET_ENV} is required for Harbor bridge tools."
        reader, writer = await asyncio.open_unix_connection(socket_path)
        payload = {"operation": self._operation, **kwargs}
        writer.write((json.dumps(payload, sort_keys=True) + "\n").encode())
        await writer.drain()
        response = json.loads((await reader.readline()).decode())
        writer.close()
        await writer.wait_closed()
        if not response.get("ok"):
            return f"Error: Harbor bridge refused {self._operation}: {response.get('error')}"
        return str(response.get("result", ""))


def register_harbor_bridge_tools(registry) -> None:
    """Register the only task-container operations available in eval mode."""
    if os.environ.get(_SOCKET_ENV) is None:
        return
    if os.environ.get("CORSIA_TEST_MODE") != "1":
        raise RuntimeError("Harbor bridge tools require CORSIA_TEST_MODE=1")
    for operation in ("exec", "read", "write", "upload", "download"):
        registry.register(HarborBridgeTool(operation))
