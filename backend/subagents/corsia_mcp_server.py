"""Thin stdio MCP client for the backend-owned Claude Code tool executor."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import uuid
from collections.abc import Awaitable, Callable
from hashlib import sha256
from typing import Any

import httpx

# Wire contract with the loopback bridge: the exact 401 detail naming a
# mid-transition token rotation. The bridge keeps a rotated-out token valid
# for a bounded dual-accept window, so one delayed retry with the same token
# normally lands on the settled successor handle. This process cannot mint or
# read the successor's token (its own token is env-injected at spawn), so
# re-presenting its token is the only refresh.
#
# Mirror of the canonical ``TOOL_TOKEN_ROTATED_DETAIL`` in
# ``subagents/cc_runtime_state.py`` (pinned equal by test). This shim runs
# standalone inside the CC subprocess and must not import backend modules, so
# the literal is duplicated here by necessity.
TOOL_TOKEN_ROTATED_DETAIL = "Claude Code tool token rotated"

# One bounded retry only, and only on the exact rotated detail. Long enough
# for a replacement handle to settle, far below the server's grace window.
_TOKEN_ROTATED_RETRY_DELAY_S = 2.0

# Mirror of the canonical ``_CC_MEDIA_KIND`` in ``core/media_delivery.py``
# (pinned equal by test). This shim runs standalone inside the CC subprocess
# and must not import backend modules, so the literal is duplicated here by
# necessity, exactly as ``TOOL_TOKEN_ROTATED_DETAIL`` above is.
MEDIA_ENVELOPE_KIND = "corsia_tool_media"


def _token_rotated(response: httpx.Response) -> bool:
    """Whether a response is the bridge's specific token-rotation 401."""
    if response.status_code != 401:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    return (
        isinstance(body, dict)
        and body.get("detail") == TOOL_TOKEN_ROTATED_DETAIL
    )


def _media_envelope(result: str, *, secret: str) -> dict[str, Any] | None:
    """Return a valid signed media envelope, never ordinary tool text."""
    if not result.startswith("{"):
        return None
    try:
        payload = json.loads(result)
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("kind") != MEDIA_ENVELOPE_KIND:
        return None
    signature = payload.pop("signature", None)
    if not isinstance(signature, str):
        return None
    unsigned = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    expected = hmac.new(
        secret.encode("utf-8"),
        unsigned.encode("utf-8"),
        sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    return payload


def tool_result_content(result: str, *, secret: str) -> list[Any]:
    """Project one signed bridge media result into MCP content blocks."""
    from mcp.types import ImageContent, TextContent

    envelope = _media_envelope(result, secret=secret)
    if envelope is None:
        return [TextContent(type="text", text=result)]
    blocks: list[Any] = [TextContent(type="text", text=str(envelope.get("text") or ""))]
    image = envelope.get("image")
    if isinstance(image, dict) and image.get("data") and image.get("mime_type"):
        blocks.append(ImageContent(
            type="image",
            data=str(image["data"]),
            mimeType=str(image["mime_type"]),
        ))
    return blocks


def mcp_tool_payloads(definitions: tuple[dict, ...] | list[dict]) -> list[dict[str, Any]]:
    """Return direct MCP tool payloads for the external harness surface."""
    payloads = []
    for definition in definitions:
        function = definition["function"]
        payloads.append({
            "name": function["name"],
            "description": function.get("description"),
            "inputSchema": function.get("parameters") or {"type": "object"},
        })
    return payloads


def _mcp_tool_definitions(definitions: tuple[dict, ...] | list[dict]):
    from mcp.types import Tool as MCPTool

    return [MCPTool(**payload) for payload in mcp_tool_payloads(definitions)]


def _http_client(token: str) -> httpx.AsyncClient:
    """Create a loopback-only client that ignores ambient proxy settings."""
    headers = {"X-CORSIA-Tool-Token": token}
    if fork_id := os.environ.get("CORSIA_FORK_ID"):
        headers["X-CORSIA-Fork-Id"] = fork_id
        headers["X-CORSIA-Tool-Token"] = os.environ.get("CORSIA_FORK_TOOL_TOKEN", token)
    return httpx.AsyncClient(
        headers=headers,
        timeout=None,
        trust_env=False,
    )


async def _send_with_rotation_retry(
    send: Callable[[], Awaitable[httpx.Response]],
) -> httpx.Response:
    """Send once, and once more when the bridge answers a mid-rotation 401.

    The live handle may rotate between attempts; within the bridge's
    dual-accept window this process's token is still honored by the settled
    replacement handle. Any other failure, including a second rotation
    response, surfaces unchanged.
    """
    response = await send()
    if _token_rotated(response):
        await asyncio.sleep(_TOKEN_ROTATED_RETRY_DELAY_S)
        response = await send()
    return response


async def _list_tools(client: httpx.AsyncClient, endpoint: str) -> list[dict]:
    """Fetch the bridge catalog; a refusal raises instead of reading as no tools."""
    response = await _send_with_rotation_retry(lambda: client.get(endpoint))
    response.raise_for_status()
    return response.json()["tools"]


async def _call_tool(
    client: httpx.AsyncClient,
    endpoint: str,
    name: str,
    arguments: dict[str, Any],
    *,
    provider_tool_call_id: str | None = None,
) -> str:
    payload = {
        "invocation_id": str(uuid.uuid4()),
        "name": name,
        "arguments": arguments,
    }
    if provider_tool_call_id:
        payload["provider_tool_call_id"] = provider_tool_call_id
    response = await _send_with_rotation_retry(
        lambda: client.post(endpoint, json=payload)
    )
    response.raise_for_status()
    return response.text


def _provider_tool_call_id() -> str | None:
    """Return Claude's exact tool-use id carried in MCP request metadata."""
    from mcp.server.lowlevel.server import request_ctx

    try:
        meta = request_ctx.get().meta
    except LookupError:
        return None
    extra = getattr(meta, "model_extra", None)
    value = extra.get("claudecode/toolUseId") if isinstance(extra, dict) else None
    return value if isinstance(value, str) and value else None


async def _run_stdio_server(
    endpoint: str,
    token: str,
) -> None:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server

    server = Server("corsia")
    client = _http_client(token)

    @server.list_tools()
    async def list_tools():
        return _mcp_tool_definitions(await _list_tools(client, endpoint))

    # The backend registry validates against these same schemas and answers a
    # violation with its own error result. The SDK's pre-check would answer it
    # here instead, so no bridge row would exist for Claude Code's echo (#1213).
    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]):
        result = await _call_tool(
            client,
            endpoint,
            name,
            arguments or {},
            provider_tool_call_id=_provider_tool_call_id(),
        )
        return tool_result_content(result, secret=token)

    async with client, stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    try:
        import mcp  # noqa: F401
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "CORSIA MCP server requires the backend mcp package. "
            "Install backend requirements before launching Claude Code."
        ) from exc

    endpoint = os.environ.get("CORSIA_CC_TOOL_URL")
    token = os.environ.get("CORSIA_CC_TOOL_TOKEN")
    if not endpoint or not token:
        raise SystemExit("CORSIA MCP loopback URL/token are required")
    asyncio.run(_run_stdio_server(endpoint, token))


if __name__ == "__main__":
    main()
