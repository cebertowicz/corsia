
from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any, AsyncIterator
from unittest.mock import patch


def _make_chunk(*, content: str | None = None, finish_reason: str | None = None,
                tool_calls: list[dict] | None = None):
    tc_payload = None
    if tool_calls:
        tc_payload = []
        for i, tc in enumerate(tool_calls):
            tc_payload.append(SimpleNamespace(
                index=i,
                id=tc.get("id", f"tc_{i}"),
                function=SimpleNamespace(
                    name=tc.get("name", ""),
                    arguments=tc.get("arguments_json", "{}"),
                ),
            ))
    delta = SimpleNamespace(
        content=content,
        reasoning_content=None,
        tool_calls=tc_payload,
    )
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=None)


def _make_non_streaming(reply: dict):
    message = SimpleNamespace(
        content=reply.get("content"),
        reasoning_content=None,
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


@contextlib.asynccontextmanager
async def mock_llm_replies(
    *, replies: list[dict],
) -> AsyncIterator[list[dict]]:
    call_count = {"n": 0}

    def _next_reply() -> dict:
        idx = min(call_count["n"], len(replies) - 1)
        call_count["n"] += 1
        return replies[idx] if replies else {}

    async def _streaming_iter(reply: dict):
        delay_ms = reply.get("delay_ms", 0) or 0
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)
        content = reply.get("content")
        if content:
            mid = len(content) // 2
            yield _make_chunk(content=content[:mid] or content)
            if mid > 0:
                yield _make_chunk(content=content[mid:])
        if reply.get("tool_calls"):
            yield _make_chunk(tool_calls=reply["tool_calls"])
        yield _make_chunk(finish_reason=reply.get("finish_reason", "stop"))

    async def fake_acompletion(**kwargs):
        reply = _next_reply()
        if kwargs.get("stream"):
            return _streaming_iter(reply)
        delay_ms = reply.get("delay_ms", 0) or 0
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)
        return _make_non_streaming(reply)

    with patch("llm.base.acompletion", new=fake_acompletion):
        yield replies
