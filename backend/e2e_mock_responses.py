"""litellm-shaped response objects the E2E mock returns."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace


def _make_chunk(
    *,
    content: str | None = None,
    reasoning_content: str | None = None,
    finish_reason: str | None = None,
    tool_calls: list[dict] | None = None,
) -> SimpleNamespace:
    """Build a litellm-style streaming chunk."""
    tc_payload = None
    if tool_calls:
        tc_payload = []
        for i, tc in enumerate(tool_calls):
            tc_payload.append(SimpleNamespace(
                index=i,
                id=tc.get("id", f"tc_{i}"),
                function=SimpleNamespace(
                    name=tc.get("name", ""),
                    arguments=tc.get("arguments_json", tc.get("arguments", "{}")),
                ),
            ))
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning_content,
        tool_calls=tc_payload,
    )
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=None)


def _make_non_streaming(reply: dict) -> SimpleNamespace:
    """Build a litellm-style non-streaming response."""
    message = SimpleNamespace(
        content=reply.get("content"),
        reasoning_content=reply.get("reasoning_content"),
        tool_calls=None,
    )
    choice = SimpleNamespace(
        message=message,
        finish_reason=reply.get("finish_reason", "stop"),
    )
    usage = SimpleNamespace(
        prompt_tokens=reply.get("prompt_tokens", 100),
        completion_tokens=reply.get("completion_tokens", 50),
        total_tokens=reply.get("total_tokens", 150),
    )
    return SimpleNamespace(choices=[choice], usage=usage)


async def _stream_tool_calls(reply: dict):
    """A reply's tool calls: whole in one chunk, or one call whose input streams in pieces (the first carries its id and name)."""
    if reply.get("tool_calls"):
        yield _make_chunk(tool_calls=reply["tool_calls"])
    stream: dict = reply.get("tool_call_stream") or {"pieces": []}
    for position, piece in enumerate(stream["pieces"]):
        if position and stream.get("delay_ms"):
            await asyncio.sleep(stream["delay_ms"] / 1000.0)
        call = SimpleNamespace(index=0, id=stream["id"] if not position else None,
                               function=SimpleNamespace(name=stream["name"] if not position else None, arguments=piece))
        yield SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content=None, reasoning_content=None, tool_calls=[call]), finish_reason=None)], usage=None)
