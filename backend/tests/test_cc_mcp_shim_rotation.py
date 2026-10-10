"""The stdio MCP shim survives one mid-rotation 401 on the list and call paths.

Issue #1137: a live-handle transition rotates the bridge's tool token. The
bridge keeps the rotated-out token valid for a bounded window and answers the
exact ``Claude Code tool token rotated`` 401 outside it. ``call_tool`` already
retried once on that detail; ``list_tools`` raised instead, so Claude Code
reported the CORSIA server ready with zero tools and ran the Turn toolless.

These tests run the real shim as a subprocess against a loopback fake bridge.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError
from mcp.types import TextContent

_SERVER = Path(__file__).resolve().parents[1] / "subagents" / "corsia_mcp_server.py"
_TOOL = "read_file"
_DEFINITIONS = [{"type": "function", "function": {
    "name": _TOOL,
    "description": "Read a file.",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
}}]
_ROTATED = {"detail": "Claude Code tool token rotated"}
_INVALID = {"detail": "Invalid Claude Code tool token"}


class _RotatingBridge:
    """Answer the first ``get_refusals``/``post_refusals`` requests with a 401."""

    def __init__(self, *, get_refusals: int = 0, post_refusals: int = 0, body: dict) -> None:
        self.gets = 0
        self.posts = 0
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                bridge.gets += 1
                if bridge.gets <= get_refusals:
                    self._reply(401, json.dumps(body).encode(), "application/json")
                    return
                self._reply(
                    200, json.dumps({"tools": _DEFINITIONS}).encode(),
                    "application/json",
                )

            def do_POST(self) -> None:  # noqa: N802
                self.rfile.read(int(self.headers["Content-Length"]))
                bridge.posts += 1
                if bridge.posts <= post_refusals:
                    self._reply(401, json.dumps(body).encode(), "application/json")
                    return
                self._reply(200, b"bridge answered", "text/plain")

            def log_message(self, *_args) -> None:
                return None

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}/tool"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> "_RotatingBridge":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def _params(bridge_url: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=[str(_SERVER)],
        env={
            "CORSIA_CC_TOOL_URL": bridge_url,
            "CORSIA_CC_TOOL_TOKEN": "shim-test-token",
        },
    )


async def _list_shim_tools(bridge_url: str) -> list[str] | McpError:
    async with stdio_client(_params(bridge_url)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            try:
                return [tool.name for tool in (await session.list_tools()).tools]
            except McpError as error:
                return error


async def _call_shim_tool(bridge_url: str) -> list[str]:
    async with stdio_client(_params(bridge_url)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(_TOOL, {"path": "a.md"})
            return [
                block.text for block in result.content
                if isinstance(block, TextContent)
            ]


@pytest.mark.asyncio
async def test_list_tools_retries_once_on_a_rotated_token():
    with _RotatingBridge(get_refusals=1, body=_ROTATED) as bridge:
        names = await _list_shim_tools(bridge.url)

    assert names == [_TOOL], "a rotated token must retry into the catalog, not an empty list"
    assert bridge.gets == 2


@pytest.mark.asyncio
async def test_call_tool_retries_once_on_a_rotated_token():
    with _RotatingBridge(post_refusals=1, body=_ROTATED) as bridge:
        texts = await _call_shim_tool(bridge.url)

    assert texts == ["bridge answered"]
    assert bridge.posts == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [_ROTATED, _INVALID], ids=["rotation-persists", "invalid-token"])
async def test_list_tools_refusal_raises_instead_of_reading_as_no_tools(body):
    with _RotatingBridge(get_refusals=99, body=body) as bridge:
        outcome = await _list_shim_tools(bridge.url)

    assert isinstance(outcome, McpError), f"a refusal read as tools: {outcome!r}"

    # Exactly one retry for the rotated detail, none for any other refusal.
    assert bridge.gets == (2 if body is _ROTATED else 1)
