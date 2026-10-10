
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm.providers.openai import openai_completion  # noqa: E402




class _AsyncEventIter:

    def __init__(self, events):
        self._events = list(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


def _ev(etype: str, **fields) -> SimpleNamespace:
    return SimpleNamespace(type=etype, **fields)


def _completed_event(*, status: str = "completed", input_tokens: int = 1,
                     output_tokens: int = 1, total_tokens: int = 2) -> SimpleNamespace:
    return _ev(
        "response.completed",
        response=SimpleNamespace(
            status=status,
            usage=SimpleNamespace(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
            ),
        ),
    )


def _make_mock_client(create_mock: AsyncMock) -> MagicMock:
    client = MagicMock(name="AsyncOpenAI")
    client.responses = MagicMock(name="responses")
    client.responses.create = create_mock
    return client


def _streaming_create_mock(events):
    mock = AsyncMock()

    async def _fresh(*_args, **_kwargs):
        return _AsyncEventIter(list(events))

    mock.side_effect = _fresh
    return mock




@pytest.mark.asyncio
async def test_callback_triggers_streaming_with_stream_kwarg():
    events = [_ev("response.output_text.delta", delta="hi"), _completed_event()]
    create_mock = _streaming_create_mock(events)
    client = _make_mock_client(create_mock)
    captured = []
    accepted = AsyncMock()
    with patch("llm.providers.openai._get_client", return_value=client):
        await openai_completion(
            messages=[{"role": "user", "content": "hi"}], tools=None,
            model="openai/gpt-5", max_tokens=100,
            on_content_chunk=lambda c: captured.append(c) or _aresult(),
            on_response_started=accepted,
        )
    assert create_mock.call_count == 1
    assert create_mock.call_args.kwargs.get("stream") is True
    accepted.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_no_callback_uses_non_streaming():
    non_streaming = SimpleNamespace(
        output_text="hi", output=[], status="completed",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
    )
    create_mock = AsyncMock(return_value=non_streaming)
    client = _make_mock_client(create_mock)
    with patch("llm.providers.openai._get_client", return_value=client):
        await openai_completion(
            messages=[{"role": "user", "content": "hi"}], tools=None,
            model="openai/gpt-5", max_tokens=100,
        )
    assert create_mock.call_count == 1
    assert create_mock.call_args.kwargs.get("stream") is None




async def _aresult():
    return None  # callbacks are awaitables; this stub satisfies that


@pytest.mark.asyncio
async def test_text_deltas_fire_content_callback_in_order():
    events = [
        _ev("response.output_text.delta", delta="hel"),
        _ev("response.output_text.delta", delta="lo "),
        _ev("response.output_text.delta", delta="world"),
        _completed_event(),
    ]
    captured: list[str] = []

    async def on_content(chunk):
        captured.append(chunk)

    client = _make_mock_client(_streaming_create_mock(events))
    with patch("llm.providers.openai._get_client", return_value=client):
        result = await openai_completion(
            messages=[{"role": "user", "content": "hi"}], tools=None,
            model="openai/gpt-5", max_tokens=100,
            on_content_chunk=on_content,
        )
    assert captured == ["hel", "lo ", "world"]
    assert result.content == "hello world"
    assert result.finish_reason == "stop"


@pytest.mark.asyncio
async def test_reasoning_deltas_fire_reasoning_callback():
    events = [
        _ev("response.reasoning_summary_text.delta", delta="thinking...", item_id="rs_1"),
        _ev("response.reasoning_summary_text.delta", delta=" more", item_id="rs_1"),
        _ev("response.output_text.delta", delta="final"),
        _completed_event(),
    ]
    reasoning_seen: list[str] = []
    content_seen: list[str] = []

    async def on_reasoning(c): reasoning_seen.append(c)
    async def on_content(c): content_seen.append(c)

    client = _make_mock_client(_streaming_create_mock(events))
    with patch("llm.providers.openai._get_client", return_value=client):
        result = await openai_completion(
            messages=[{"role": "user", "content": "hi"}], tools=None,
            model="openai/gpt-5", max_tokens=100,
            on_reasoning_chunk=on_reasoning, on_content_chunk=on_content,
        )
    assert reasoning_seen == ["thinking...", " more"]
    assert content_seen == ["final"]
    assert result.reasoning_content == "thinking... more"
    assert result.reasoning_provenance == {
        "representation_kind": "opaque",
        "native_reference": "rs_1",
        "native_references": ["rs_1"],
    }
    assert result.content == "final"


@pytest.mark.asyncio
async def test_streaming_reasoning_references_preserve_first_seen_order_and_dedupe():
    events = [
        _ev(
            "response.output_item.added",
            item=SimpleNamespace(type="reasoning", id="rs_second"),
        ),
        _ev(
            "response.reasoning_summary_text.delta",
            delta="first",
            item_id="rs_first",
        ),
        _ev(
            "response.reasoning_summary_text.delta",
            delta=" second",
            item_id="rs_second",
        ),
        _ev(
            "response.reasoning_summary_text.delta",
            delta=" duplicate",
            item_id="rs_first",
        ),
        _completed_event(),
    ]
    client = _make_mock_client(_streaming_create_mock(events))

    async def on_reasoning(_chunk):
        return None

    with patch("llm.providers.openai._get_client", return_value=client):
        result = await openai_completion(
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            model="openai/gpt-5",
            max_tokens=100,
            on_reasoning_chunk=on_reasoning,
        )

    assert result.reasoning_content == "first second duplicate"
    assert result.reasoning_provenance == {
        "representation_kind": "opaque",
        "native_references": ["rs_second", "rs_first"],
    }


@pytest.mark.asyncio
async def test_openai_adapter_classifies_reasoning_summary_reference():
    reasoning_item = SimpleNamespace(
        type="reasoning",
        id="rs_123",
        summary=[SimpleNamespace(text="provider summary")],
        encrypted_content="opaque-provider-state",
    )
    non_streaming = SimpleNamespace(
        output_text="answer",
        output=[reasoning_item],
        status="completed",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
    )
    create_mock = AsyncMock(return_value=non_streaming)
    client = _make_mock_client(create_mock)

    with patch("llm.providers.openai._get_client", return_value=client):
        result = await openai_completion(
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            model="openai/gpt-5",
            max_tokens=100,
        )

    assert result.reasoning_content == "provider summary"
    assert result.reasoning_provenance == {
        "representation_kind": "opaque",
        "native_reference": "rs_123",
        "native_references": ["rs_123"],
    }


@pytest.mark.asyncio
async def test_openai_adapter_preserves_ordered_deduped_reasoning_references():
    reasoning_items = [
        SimpleNamespace(
            type="reasoning",
            id="rs_second",
            summary=[SimpleNamespace(text="second")],
        ),
        SimpleNamespace(
            type="reasoning",
            id="rs_first",
            summary=[SimpleNamespace(text="first")],
        ),
        SimpleNamespace(
            type="reasoning",
            id="rs_second",
            summary=[SimpleNamespace(text="duplicate")],
        ),
    ]
    non_streaming = SimpleNamespace(
        output_text="answer",
        output=reasoning_items,
        status="completed",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
    )
    client = _make_mock_client(AsyncMock(return_value=non_streaming))

    with patch("llm.providers.openai._get_client", return_value=client):
        result = await openai_completion(
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            model="openai/gpt-5",
            max_tokens=100,
        )

    assert result.reasoning_content == "second\nfirst\nduplicate"
    assert result.reasoning_provenance == {
        "representation_kind": "opaque",
        "native_references": ["rs_second", "rs_first"],
    }


@pytest.mark.asyncio
async def test_empty_deltas_dont_invoke_callbacks():
    events = [
        _ev("response.output_text.delta", delta=""),
        _ev("response.output_text.delta", delta=None),
        _ev("response.output_text.delta", delta="real"),
        _completed_event(),
    ]
    seen: list[str] = []

    async def on_content(c): seen.append(c)

    client = _make_mock_client(_streaming_create_mock(events))
    with patch("llm.providers.openai._get_client", return_value=client):
        await openai_completion(
            messages=[{"role": "user", "content": "hi"}], tools=None,
            model="openai/gpt-5", max_tokens=100,
            on_content_chunk=on_content,
        )
    assert seen == ["real"]




@pytest.mark.asyncio
async def test_function_call_assembled_from_added_plus_arg_deltas():
    item = SimpleNamespace(
        type="function_call", call_id="call_123", id="fc_1",
        name="search", arguments="",
    )
    events = [
        _ev("response.output_item.added", output_index=0, item=item),
        _ev("response.function_call_arguments.delta", output_index=0, item_id="fc_1",
            delta='{"q":'),
        _ev("response.function_call_arguments.delta", output_index=0, item_id="fc_1",
            delta=' "cats"}'),
        _ev("response.output_item.done", output_index=0,
            item=SimpleNamespace(type="function_call", call_id="call_123",
                                 name="search", arguments='{"q": "cats"}')),
        _completed_event(),
    ]

    async def on_content(c): pass

    client = _make_mock_client(_streaming_create_mock(events))
    with patch("llm.providers.openai._get_client", return_value=client):
        result = await openai_completion(
            messages=[{"role": "user", "content": "hi"}], tools=None,
            model="openai/gpt-5", max_tokens=100,
            on_content_chunk=on_content,
        )
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.id == "call_123"
    assert tc.name == "search"
    assert tc.arguments == {"q": "cats"}


@pytest.mark.asyncio
async def test_function_call_recovers_when_added_event_missing():
    events = [
        _ev("response.function_call_arguments.delta", output_index=0,
            item_id="fc_1", delta='{"x": 1}'),
        _ev("response.output_item.done", output_index=0,
            item=SimpleNamespace(type="function_call", call_id="call_xyz",
                                 name="lookup", arguments='{"x": 1}')),
        _completed_event(),
    ]

    async def on_content(c): pass

    client = _make_mock_client(_streaming_create_mock(events))
    with patch("llm.providers.openai._get_client", return_value=client):
        result = await openai_completion(
            messages=[{"role": "user", "content": "hi"}], tools=None,
            model="openai/gpt-5", max_tokens=100,
            on_content_chunk=on_content,
        )
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.id == "call_xyz"
    assert tc.name == "lookup"
    assert tc.arguments == {"x": 1}




@pytest.mark.asyncio
async def test_response_incomplete_yields_finish_reason_length():
    events = [
        _ev("response.output_text.delta", delta="partial"),
        _ev(
            "response.incomplete",
            response=SimpleNamespace(
                status="incomplete",
                usage=SimpleNamespace(input_tokens=10, output_tokens=100, total_tokens=110),
            ),
        ),
    ]

    async def on_content(c): pass

    client = _make_mock_client(_streaming_create_mock(events))
    with patch("llm.providers.openai._get_client", return_value=client):
        result = await openai_completion(
            messages=[{"role": "user", "content": "hi"}], tools=None,
            model="openai/gpt-5", max_tokens=100,
            on_content_chunk=on_content,
        )
    assert result.finish_reason == "length"
    assert result.content == "partial"


@pytest.mark.asyncio
async def test_completed_with_status_incomplete_also_yields_length():
    events = [
        _ev("response.output_text.delta", delta="cut"),
        _ev(
            "response.completed",
            response=SimpleNamespace(
                status="incomplete",
                usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
            ),
        ),
    ]

    async def on_content(c): pass

    client = _make_mock_client(_streaming_create_mock(events))
    with patch("llm.providers.openai._get_client", return_value=client):
        result = await openai_completion(
            messages=[{"role": "user", "content": "hi"}], tools=None,
            model="openai/gpt-5", max_tokens=100,
            on_content_chunk=on_content,
        )
    assert result.finish_reason == "length"




@pytest.mark.asyncio
async def test_response_failed_returns_error_response():
    events = [
        _ev(
            "response.failed",
            response=SimpleNamespace(error="rate_limited"),
        ),
    ]

    async def on_content(c): pass

    create_mock = _streaming_create_mock(events)
    client = _make_mock_client(create_mock)
    with patch("llm.providers.openai._get_client", return_value=client), \
         patch("llm.providers.openai.asyncio.sleep", new_callable=AsyncMock):
        result = await openai_completion(
            messages=[{"role": "user", "content": "hi"}], tools=None,
            model="openai/gpt-5", max_tokens=100,
            on_content_chunk=on_content,
        )
    assert result.finish_reason == "error"
    assert result.content and result.content.startswith("Error calling LLM")


@pytest.mark.asyncio
async def test_usage_pulled_from_completed_event():
    events = [
        _ev("response.output_text.delta", delta="ok"),
        _completed_event(input_tokens=42, output_tokens=7, total_tokens=49),
    ]

    async def on_content(c): pass

    client = _make_mock_client(_streaming_create_mock(events))
    with patch("llm.providers.openai._get_client", return_value=client):
        result = await openai_completion(
            messages=[{"role": "user", "content": "hi"}], tools=None,
            model="openai/gpt-5", max_tokens=100,
            on_content_chunk=on_content,
        )
    assert result.usage["prompt_tokens"] == 42
    assert result.usage["completion_tokens"] == 7
    assert result.usage["total_tokens"] == 49
