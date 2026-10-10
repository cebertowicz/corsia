"""A native provider attempt that does not return a streamed call ends that call's preview at once.

The adapter retries a failed attempt itself and turns its last failure into an
error response, so no exception reaches the Turn: the attempt that streamed the
input is the only place that knows the call will never run.
"""
from types import SimpleNamespace

import pytest

from doceditor import live_preview
from doceditor.live_preview import PreviewRoute
from tests.test_live_preview_sources import anthropic_events


class _Stream:
    def __init__(self, events: list, tail: str) -> None:
        self.events = events
        self.tail = tail

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.events:
            return self.events.pop(0)
        if self.tail == "raise":
            raise RuntimeError("connection reset by peer")
        raise StopAsyncIteration

    async def aclose(self) -> None:
        return None


@pytest.fixture
def route(monkeypatch):
    route = PreviewRoute()
    monkeypatch.setattr(live_preview, "previews", route)
    return route


async def _attempt(tail: str, *, whole: bool):
    from core.turn_provider_call import _StreamingCallbacks
    from llm.providers.anthropic_streaming import _streaming_completion
    from llm.tool_input_stream import tool_input_sink

    async def ignore(*_args):
        return None

    events = anthropic_events("call-1", 0, {"path": "a.md", "old_string": "a", "new_string": "typed"})
    if whole:
        events += [SimpleNamespace(type="message_delta", delta=SimpleNamespace(stop_reason="tool_use"), usage=None),
                   SimpleNamespace(type="message_stop")]
    else:
        events = events[:4]  # the input stops part way through
    stream = _Stream(events, tail)

    async def create(**_kwargs):
        return stream

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    binding = tool_input_sink.set(_StreamingCallbacks(ignore, "thread-a"))
    try:
        return await _streaming_completion(client, {}, model="claude-test", on_reasoning_chunk=ignore,
                                           on_content_chunk=ignore, on_tool_call_chunk=ignore)
    finally:
        tool_input_sink.reset(binding)


async def test_an_attempt_that_fails_mid_input_ends_its_preview(route):
    with pytest.raises(RuntimeError):
        await _attempt("raise", whole=False)
    assert route.calls == {} and "call-1" in route.ended


async def test_an_attempt_cut_off_mid_input_ends_its_preview(route):
    response = await _attempt("end", whole=False)
    assert response.tool_calls == []
    assert route.calls == {} and "call-1" in route.ended


async def test_a_returned_call_keeps_its_preview_until_its_write_ends_it(route):
    response = await _attempt("end", whole=True)
    assert [call.id for call in response.tool_calls] == ["call-1"]
    assert set(route.calls) == {"call-1"}
