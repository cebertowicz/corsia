"""The stdio MCP shim forwards every declared call; the registry alone validates.

Issue #1213: the MCP SDK checks a call's arguments against the advertised
schema before the shim's handler runs. A schema-invalid call was therefore
answered inside the shim and never reached the backend bridge, so CORSIA wrote
no tool result for it. Claude Code still echoed the SDK's error, and the echo
had no result to bind to: ``provider tool-result echo does not identify one
canonical result``. CORSIA's own registry already validates the same schema
and turns a violation into a bound error row, so the SDK check is a second
authority that only loses the call.

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

_SERVER = Path(__file__).resolve().parents[1] / "subagents" / "corsia_mcp_server.py"
_TOOL = "strict_tool"
_DEFINITIONS = [{"type": "function", "function": {
    "name": _TOOL,
    "description": "A tool whose schema rejects unknown keys.",
    "parameters": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    },
}}]


class _FakeBridge:
    """Serve the tool list and record every call the shim forwards."""

    def __init__(self) -> None:
        self.posts: list[dict] = []
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, body: bytes, content_type: str) -> None:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                self._reply(
                    json.dumps({"tools": _DEFINITIONS}).encode(),
                    "application/json",
                )

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers["Content-Length"])
                bridge.posts.append(json.loads(self.rfile.read(length)))
                self._reply(b"bridge answered", "text/plain")

            def log_message(self, *_args) -> None:
                return None

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}/tool"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> "_FakeBridge":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


async def _call_shim(bridge_url: str, arguments: dict):
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(_SERVER)],
        env={
            "CORSIA_CC_TOOL_URL": bridge_url,
            "CORSIA_CC_TOOL_TOKEN": "shim-test-token",
        },
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(
                _TOOL, arguments, meta={"claudecode/toolUseId": "provider-call-1"},
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({}, id="missing-required"),
        pytest.param({"path": "a.md", "extra": 1}, id="unexpected-key"),
        pytest.param({"path": 7}, id="wrong-type"),
    ],
)
async def test_schema_invalid_call_still_reaches_the_bridge_once(arguments):
    """Only the backend registry may refuse a call, so a refusal gets a result row."""
    with _FakeBridge() as bridge:
        result = await _call_shim(bridge.url, arguments)

    assert [
        (post["name"], post["arguments"], post["provider_tool_call_id"])
        for post in bridge.posts
    ] == [(_TOOL, arguments, "provider-call-1")]
    assert [block.text for block in result.content] == ["bridge answered"]


@pytest.mark.asyncio
async def test_valid_call_reaches_the_bridge_once():
    with _FakeBridge() as bridge:
        result = await _call_shim(bridge.url, {"path": "a.md"})

    assert [post["arguments"] for post in bridge.posts] == [{"path": "a.md"}]
    assert [block.text for block in result.content] == ["bridge answered"]
