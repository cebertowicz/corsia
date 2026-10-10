
from __future__ import annotations

import asyncio
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _make_chunk(content: str | None = None, finish_reason: str | None = None):
    delta = SimpleNamespace(
        content=content,
        reasoning_content=None,
        tool_calls=None,
    )
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    chunk = SimpleNamespace(choices=[choice], usage=None)
    return chunk


async def _mock_stream_with_delay(delay_ms: int):
    await asyncio.sleep(delay_ms / 1000.0)
    yield _make_chunk(content="hello ")
    yield _make_chunk(content="world")
    yield _make_chunk(finish_reason="stop")


class _ClosableBlockingStream:
    def __init__(self, *chunks, close_error: BaseException | None = None):
        self._chunks = list(chunks)
        self._close_error = close_error
        self.next_blocked = asyncio.Event()
        self.closed = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._chunks:
            return self._chunks.pop(0)
        self.next_blocked.set()
        await asyncio.Future()

    async def aclose(self):
        self.closed.set()
        if self._close_error is not None:
            raise self._close_error


def _sse_chunk(content: str | None, finish_reason: str | None = None) -> bytes:
    payload = {
        "id": "chatcmpl-native-stop",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "native-stop-test",
        "choices": [{
            "index": 0,
            "delta": {"content": content} if content is not None else {},
            "finish_reason": finish_reason,
        }],
    }
    if finish_reason is not None:
        payload["usage"] = {
            "prompt_tokens": 8,
            "completion_tokens": 1,
            "total_tokens": 9,
        }
    return f"data: {json.dumps(payload)}\n\n".encode()


class _NativeSSEProbe:
    def __init__(self, cancel_at: str):
        self.cancel_at = cancel_at
        self.requests: list[dict] = []
        self.prefill_blocked = asyncio.Event()
        self.disconnected = asyncio.Event()

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(await request.json())
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        if len(self.requests) == 1:
            if self.cancel_at == "callback":
                await response.write(_sse_chunk("partial"))
            self.prefill_blocked.set()
            await self._observe_disconnect(response)
            return response
        await response.write(_sse_chunk("successor"))
        await response.write(_sse_chunk(None, "stop"))
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response

    async def _observe_disconnect(self, response: web.StreamResponse) -> None:
        while True:
            try:
                await response.write(b": provider-active\n\n")
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                self.disconnected.set()
                raise
            except ConnectionResetError:
                self.disconnected.set()
                return


async def _start_native_sse_probe(cancel_at: str) -> tuple[_NativeSSEProbe, TestServer]:
    probe = _NativeSSEProbe(cancel_at)
    app = web.Application()
    async def loaded_status(_request):
        return web.json_response({
            "models": [{"id": "native-stop-test", "loaded": True}],
        })

    app.router.add_get("/v1/models/status", loaded_status)
    app.router.add_post("/v1/chat/completions", probe.handle)
    server = TestServer(app, host="127.0.0.1", port=0)
    await server.start_server()
    return probe, server


@pytest.mark.asyncio
async def test_ttft_ms_captured_from_first_content_delta():
    from llm.base import _streaming_completion

    delay_ms = 50

    async def fake_acompletion(**_kwargs):
        return _mock_stream_with_delay(delay_ms)

    with patch("llm.base.acompletion", new=fake_acompletion):
        captured_chunks: list[str] = []

        async def on_content(chunk: str):
            captured_chunks.append(chunk)

        response = await _streaming_completion(
            kwargs={"messages": [{"role": "user", "content": "hi"}]},
            model="test/model",
            on_reasoning_chunk=None,
            on_content_chunk=on_content,
            on_tool_call_chunk=None,
        )

    assert response.content == "hello world"
    assert captured_chunks == ["hello ", "world"]
    assert response.ttft_ms is not None
    assert response.ttft_ms >= delay_ms - 5, (
        f"TTFT={response.ttft_ms}ms expected >= {delay_ms - 5}ms"
    )
    assert response.ttft_ms < 5000


@pytest.mark.asyncio
async def test_ttft_ms_none_when_no_content_arrives():
    from llm.base import _streaming_completion

    async def empty_stream():
        yield _make_chunk(finish_reason="stop")

    async def fake_acompletion(**_kwargs):
        return empty_stream()

    with patch("llm.base.acompletion", new=fake_acompletion):
        response = await _streaming_completion(
            kwargs={"messages": [{"role": "user", "content": "hi"}]},
            model="test/model",
            on_reasoning_chunk=None,
            on_content_chunk=None,
            on_tool_call_chunk=None,
        )

    assert response.ttft_ms is None


@pytest.mark.parametrize("close_error", [None, RuntimeError("close failed")])
@pytest.mark.asyncio
async def test_cancel_during_prefill_closes_provider_stream_before_propagating(
    close_error,
):
    from llm.base import _streaming_completion

    stream = _ClosableBlockingStream(close_error=close_error)

    async def fake_acompletion(**_kwargs):
        return stream

    with patch("llm.base.acompletion", new=fake_acompletion):
        completion = asyncio.create_task(_streaming_completion(
            kwargs={"messages": [{"role": "user", "content": "hi"}]},
            model="test/model",
            on_reasoning_chunk=None,
            on_content_chunk=None,
            on_tool_call_chunk=None,
        ))
        await asyncio.wait_for(stream.next_blocked.wait(), timeout=1)
        completion.cancel()
        with pytest.raises(asyncio.CancelledError):
            await completion

    assert stream.closed.is_set()


@pytest.mark.asyncio
async def test_cancel_during_content_callback_closes_provider_stream_first():
    from llm.base import _streaming_completion

    stream = _ClosableBlockingStream(_make_chunk(content="hello"))
    callback_blocked = asyncio.Event()

    async def fake_acompletion(**_kwargs):
        return stream

    async def on_content(_chunk: str):
        callback_blocked.set()
        await asyncio.Future()

    with patch("llm.base.acompletion", new=fake_acompletion):
        completion = asyncio.create_task(_streaming_completion(
            kwargs={"messages": [{"role": "user", "content": "hi"}]},
            model="test/model",
            on_reasoning_chunk=None,
            on_content_chunk=on_content,
            on_tool_call_chunk=None,
        ))
        await asyncio.wait_for(callback_blocked.wait(), timeout=1)
        completion.cancel()
        with pytest.raises(asyncio.CancelledError):
            await completion

    assert stream.closed.is_set()


@pytest.mark.parametrize("cancel_at", ["prefill", "callback"])
@pytest.mark.asyncio
async def test_native_cancel_disconnects_provider_and_preserves_successor_prefix(cancel_at):
    from llm.router import chat_completion

    messages = [
        {"role": "system", "content": "stable cache prefix"},
        {"role": "user", "content": "keep this exact tail"},
    ]
    tools = [{
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "Look up a value.",
            "parameters": {"type": "object", "properties": {}},
        },
    }]
    probe, server = await _start_native_sse_probe(cancel_at)
    callback_blocked = asyncio.Event()
    successor_chunks: list[str] = []

    async def on_content(chunk: str):
        if chunk == "partial":
            callback_blocked.set()
            await asyncio.Future()
        successor_chunks.append(chunk)

    async def complete():
        return await chat_completion(
            messages, tools, model="openai/native-stop-test",
            api_key="omlx", api_base=str(server.make_url("/v1")).rstrip("/"),
            on_content_chunk=on_content, retry_transient_errors=False,
        )

    cancelled = asyncio.create_task(complete())
    try:
        boundary = callback_blocked if cancel_at == "callback" else probe.prefill_blocked
        # This crosses a real loopback HTTP boundary. Under shared-runner load,
        # scheduling the request can exceed two seconds before the probe sees it.
        await asyncio.wait_for(boundary.wait(), timeout=5)
        cancelled.cancel()
        await asyncio.wait_for(probe.disconnected.wait(), timeout=2)
        with pytest.raises(asyncio.CancelledError):
            await cancelled

        successor = await asyncio.wait_for(complete(), timeout=2)
    finally:
        if not cancelled.done():
            cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        await server.close()

    assert successor.content == "successor"
    assert successor_chunks == ["successor"]
    assert probe.requests[0] == probe.requests[1]
    assert probe.requests[1]["messages"] == messages
    assert probe.requests[1]["tools"] == tools
