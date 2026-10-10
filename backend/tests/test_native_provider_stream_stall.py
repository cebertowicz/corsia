"""#2333 — a provider call that goes silent ends the Native Turn as an error.

The transport's read timeout is per socket read, so heartbeat or keepalive bytes
keep it from ever cutting off while no delta reaches the Turn. The stream assembly
bounds silence on one call instead, and the stalled call fails into the Turn's
own error path, whose terminal settlement then frees the thread for queued input.

#2485 extends the same seam to a stream that ends *early*: a provider stream that
closes before its terminal event is retried before any output, and classified as a
terminal error once output reached the Turn, exactly like the silent stall. The
litellm half must not read the wrapper's synthesized final chunk as the provider's
own marker, so it is proved end to end over a real loopback SSE upstream.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

THREAD = "stalled-stream-thread"
IDLE_S = 0.05
BOUND_S = 3.0  # a test must fail by timing out here, never hang the suite


def _chunk(text: str):
    delta = SimpleNamespace(content=text, reasoning_content=None, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=None)], usage=None,
    )


def _finished_chunk(text: str):
    chunk = _chunk(text)
    chunk.choices[0].finish_reason = "stop"
    return chunk


class _Stream:
    """A provider stream with scripted behaviour after its first chunks."""

    def __init__(self, chunks, tail: str) -> None:
        self._chunks = list(chunks)
        self._tail = tail
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._chunks:
            return self._chunks.pop(0)
        if self._tail == "end":  # connection dropped without an error
            raise StopAsyncIteration
        await asyncio.Event().wait()  # accepted, then never another byte

    async def aclose(self) -> None:
        self.closed = True


def _acompletion(stream: _Stream | None):
    """``stream=None`` models a provider that never answers the request itself."""

    async def acompletion(**_kwargs):
        if stream is None:
            await asyncio.Event().wait()
        return stream

    return acompletion


@pytest.fixture(autouse=True)
def short_idle(monkeypatch):
    from infra import constants

    monkeypatch.setattr(constants, "LLM_STREAM_IDLE_TIMEOUT_S", IDLE_S)
    delays = list(constants.LLM_RETRY_DELAYS)
    constants.LLM_RETRY_DELAYS[:] = [0] * len(delays)  # the ladder holds this list
    yield
    constants.LLM_RETRY_DELAYS[:] = delays


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["never_a_byte", "no_response"])
async def test_stall_before_any_delta_is_raised_for_the_retry_ladder(scenario):
    from llm.base_streaming import streaming_completion
    from llm.stream_idle import ProviderStreamStalled

    stream = None if scenario == "no_response" else _Stream([], tail="hang")
    with pytest.raises(ProviderStreamStalled):
        await asyncio.wait_for(
            streaming_completion(
                {"model": "openai/m"}, "openai/m", None, None,
                acompletion_fn=_acompletion(stream),
            ),
            timeout=BOUND_S,
        )
    if stream is not None:
        assert stream.closed


@pytest.mark.asyncio
async def test_stall_after_a_delta_ends_the_call_as_a_classified_error():
    from llm.base_streaming import streaming_completion
    from llm.stream_idle import STALLED_ERROR_KIND

    stream = _Stream([_chunk("partial")], tail="hang")
    response = await asyncio.wait_for(
        streaming_completion(
            {"model": "openai/m"}, "openai/m", _sink, _sink,
            acompletion_fn=_acompletion(stream),
        ),
        timeout=BOUND_S,
    )
    assert (response.finish_reason, response.error_kind) == ("error", STALLED_ERROR_KIND)
    assert stream.closed


async def _sink(_text) -> None:
    """A consumer callback: the Turn is listening."""


def _ladder(monkeypatch, streams, *, callbacks: bool = True):
    """Drive the real retry ladder; each call to the provider takes the next stream."""
    import llm.base as llm_base

    calls: list[int] = []

    async def acompletion(**_kwargs):
        calls.append(1)
        return streams.pop(0)

    monkeypatch.setattr(llm_base, "acompletion", acompletion)

    async def run():
        return await asyncio.wait_for(
            llm_base._completion_with_retries(
                {"model": "openai/m"}, model="openai/m", use_streaming=True,
                on_reasoning_chunk=_sink if callbacks else None,
                on_content_chunk=_sink if callbacks else None,
                on_tool_call_chunk=None, on_response_started=None,
                retry_transient_errors=True,
            ),
            timeout=BOUND_S,
        )

    return calls, run


@pytest.mark.asyncio
async def test_silent_stall_is_retried_and_recovers(monkeypatch):
    ok = _Stream([_finished_chunk("hello")], tail="end")
    first = _Stream([], tail="hang")
    calls, run = _ladder(monkeypatch, [first, ok])
    response = await run()
    assert len(calls) == 2 and first.closed and ok.closed
    assert response.content == "hello" and response.finish_reason == "stop"


@pytest.mark.asyncio
async def test_silent_stalls_exhaust_the_ladder_as_a_classified_error(monkeypatch):
    from infra import constants
    from llm.stream_idle import STALLED_ERROR_KIND

    calls, run = _ladder(monkeypatch, [_Stream([], tail="hang") for _ in range(9)])
    response = await run()
    assert len(calls) == constants.LLM_MAX_RETRIES
    assert response.error_kind == STALLED_ERROR_KIND


@pytest.mark.asyncio
async def test_non_output_chunks_do_not_reset_the_deadline(monkeypatch):
    """The production path (litellm via base_streaming): empty deltas are not progress."""
    from infra import constants
    from llm.stream_idle import STALLED_ERROR_KIND

    class EmptyChunks(_Stream):
        async def __anext__(self):
            await asyncio.sleep(IDLE_S / 5)
            return _chunk(None)  # a delta with no content, reasoning or tool call

    streams = [EmptyChunks([], tail="hang") for _ in range(constants.LLM_MAX_RETRIES)]
    calls, run = _ladder(monkeypatch, list(streams))
    response = await run()
    assert len(calls) == constants.LLM_MAX_RETRIES
    assert response.error_kind == STALLED_ERROR_KIND
    assert all(stream.closed for stream in streams)


@pytest.mark.asyncio
async def test_responses_stall_after_a_tool_header_only_is_still_retryable():
    """A function_call header fills state but reached nothing, so the stall is raised."""
    from llm.providers.openai_streaming import _streaming_responses_completion
    from llm.stream_idle import ProviderStreamStalled

    item = SimpleNamespace(type="function_call", call_id="c1", name="tool", arguments="")
    header = SimpleNamespace(type="response.output_item.added", item=item, output_index=0)
    stream = _Stream([header], tail="hang")

    async def create(**_kwargs):
        return stream

    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    with pytest.raises(ProviderStreamStalled):
        await asyncio.wait_for(
            _streaming_responses_completion(
                client, {}, model="openai/m", on_reasoning_chunk=None,
                on_content_chunk=None, on_tool_call_chunk=None,
            ),
            timeout=BOUND_S,
        )


@pytest.mark.asyncio
async def test_stall_after_a_delta_is_never_retried(monkeypatch):
    stream = _Stream([_chunk("partial")], tail="hang")
    calls, run = _ladder(monkeypatch, [stream, _Stream([_finished_chunk("x")], tail="end")])
    response = await run()
    assert len(calls) == 1 and response.finish_reason == "error"


@pytest.mark.asyncio
async def test_stall_after_a_token_with_no_consumer_is_retried(monkeypatch):
    """A callback-less caller (checkpoint fork) saw nothing, so the stall is retryable."""
    first = _Stream([_chunk("partial")], tail="hang")
    calls, run = _ladder(
        monkeypatch, [first, _Stream([_finished_chunk("hello")], tail="end")],
        callbacks=False,
    )
    response = await run()
    assert len(calls) == 2 and response.content == "hello"


@pytest.mark.asyncio
async def test_started_callback_failure_closes_the_stream_on_both_native_loops():
    from llm.providers.anthropic_streaming import _streaming_completion
    from llm.providers.openai_streaming import _streaming_responses_completion

    async def boom():
        raise RuntimeError("stopped")

    for run, client_attr in (
        (_streaming_completion, "messages"), (_streaming_responses_completion, "responses"),
    ):
        stream = _Stream([], tail="hang")

        async def create(**_kwargs):
            return stream

        client = SimpleNamespace(**{client_attr: SimpleNamespace(create=create)})
        kwargs = {"model": "m", "on_reasoning_chunk": None, "on_content_chunk": None,
                  "on_tool_call_chunk": None, "on_response_started": boom}
        with pytest.raises(RuntimeError, match="stopped"):
            await asyncio.wait_for(run(client, {}, **kwargs), timeout=BOUND_S)
        assert stream.closed, client_attr


@pytest.mark.asyncio
async def test_stream_is_closed_when_the_consumer_fails():
    from llm.base_streaming import streaming_completion

    async def boom(_text):
        raise RuntimeError("consumer failed")

    stream = _Stream([_finished_chunk("x")], tail="end")
    with pytest.raises(RuntimeError, match="consumer failed"):
        await streaming_completion(
            {"model": "openai/m"}, "openai/m", None, boom,
            acompletion_fn=_acompletion(stream),
        )
    assert stream.closed


@pytest.mark.asyncio
async def test_steady_deltas_are_never_cut_off_by_the_silence_bound():
    from llm.base_streaming import streaming_completion

    class Slow(_Stream):
        async def __anext__(self):
            if self._chunks:
                await asyncio.sleep(IDLE_S / 2)
            return await super().__anext__()

    stream = Slow([_chunk(str(i)) for i in range(7)] + [_finished_chunk("7")], tail="end")
    response = await asyncio.wait_for(
        streaming_completion(
            {"model": "openai/m"}, "openai/m", None, None,
            acompletion_fn=_acompletion(stream),
        ),
        timeout=BOUND_S,
    )
    assert response.content == "01234567"


@pytest.mark.asyncio
async def test_anthropic_heartbeats_do_not_count_as_progress():
    from llm.providers.anthropic_streaming import _streaming_completion
    from llm.stream_idle import ProviderStreamStalled

    class Pings:
        def __init__(self) -> None:
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(IDLE_S / 5)
            return SimpleNamespace(type="ping")

        async def close(self) -> None:
            self.closed = True

    class Client:
        messages = SimpleNamespace(create=None)

    pings = Pings()

    async def create(**_kwargs):
        return pings

    client = Client()
    client.messages = SimpleNamespace(create=create)
    with pytest.raises(ProviderStreamStalled):
        await asyncio.wait_for(
            _streaming_completion(
                client, {}, model="anthropic/m", on_reasoning_chunk=None,
                on_content_chunk=None, on_tool_call_chunk=None,
            ),
            timeout=BOUND_S,
        )
    assert pings.closed


@pytest.mark.asyncio
async def test_anthropic_stall_after_output_ends_the_call_before_it_retries():
    from llm.providers.anthropic_streaming import _streaming_completion
    from llm.stream_idle import STALLED_ERROR_KIND

    delta = SimpleNamespace(type="text_delta", text="partial")
    event = SimpleNamespace(type="content_block_delta", index=0, delta=delta)

    class OneThenSilence(_Stream):
        async def close(self) -> None:
            self.closed = True

    stream = OneThenSilence([event], tail="hang")

    async def create(**_kwargs):
        return stream

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    response = await asyncio.wait_for(
        _streaming_completion(
            client, {}, model="anthropic/m", on_reasoning_chunk=_sink,
            on_content_chunk=_sink, on_tool_call_chunk=None,
        ),
        timeout=BOUND_S,
    )
    assert response.error_kind == STALLED_ERROR_KIND
    assert stream.closed


@pytest.mark.asyncio
async def test_openai_responses_stream_bounds_silence():
    """The third native dispatch (OpenAI Responses) is bounded the same way."""
    from llm.providers.openai_streaming import _streaming_responses_completion
    from llm.stream_idle import ProviderStreamStalled

    class NeverYields:
        def __init__(self) -> None:
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()

        async def close(self) -> None:
            self.closed = True

    response_iter = NeverYields()

    async def create(**_kwargs):
        return response_iter

    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    with pytest.raises(ProviderStreamStalled):
        await asyncio.wait_for(
            _streaming_responses_completion(
                client, {}, model="openai/m", on_reasoning_chunk=None,
                on_content_chunk=None, on_tool_call_chunk=None,
            ),
            timeout=BOUND_S,
        )
    assert response_iter.closed


@pytest.mark.asyncio
async def test_responses_status_events_are_not_progress():
    from llm.providers.openai_streaming import _streaming_responses_completion
    from llm.stream_idle import ProviderStreamStalled

    class Statuses(_Stream):
        async def __anext__(self):
            await asyncio.sleep(IDLE_S / 5)
            return SimpleNamespace(type="response.in_progress")

    stream = Statuses([], tail="hang")

    async def create(**_kwargs):
        return stream

    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    with pytest.raises(ProviderStreamStalled):
        await asyncio.wait_for(
            _streaming_responses_completion(
                client, {}, model="openai/m", on_reasoning_chunk=None,
                on_content_chunk=None, on_tool_call_chunk=None,
            ),
            timeout=BOUND_S,
        )
    assert stream.closed


@pytest.mark.asyncio
async def test_silence_after_the_finish_marker_keeps_the_complete_answer():
    """Litellm, Anthropic and Responses: a finished answer is not thrown away."""
    from llm.base_streaming import streaming_completion
    from llm.providers.anthropic_streaming import _streaming_completion
    from llm.providers.openai_streaming import _streaming_responses_completion

    text = SimpleNamespace(type="text_delta", text="done")
    anthropic_events = [
        SimpleNamespace(type="content_block_delta", index=0, delta=text),
        SimpleNamespace(
            type="message_delta", delta=SimpleNamespace(stop_reason="end_turn"), usage=None,
        ),
    ]
    responses_events = [
        SimpleNamespace(type="response.output_text.delta", delta="done"),
        SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(usage=None, status="completed"),
        ),
    ]

    def make(events):
        stream = _Stream(events, tail="hang")

        async def create(**_kwargs):
            return stream

        return create

    callbacks = {"on_reasoning_chunk": _sink, "on_content_chunk": _sink, "on_tool_call_chunk": None}
    results = [
        await streaming_completion(
            {"model": "openai/m"}, "openai/m", _sink, _sink,
            acompletion_fn=_acompletion(_Stream([_finished_chunk("done")], tail="hang")),
        ),
        await _streaming_completion(
            SimpleNamespace(messages=SimpleNamespace(create=make(anthropic_events))),
            {}, model="anthropic/m", **callbacks,
        ),
        await _streaming_responses_completion(
            SimpleNamespace(responses=SimpleNamespace(create=make(responses_events))),
            {}, model="openai/m", **callbacks,
        ),
    ]
    for response in results:
        assert response.error_kind is None and response.finish_reason != "error"
        assert response.content == "done"


@pytest.mark.asyncio
async def test_openai_responses_retries_classify_the_stall_the_same_way():
    """No second mechanism: the Responses loop settles through cut_off_response too."""
    from llm.providers.openai import _responses_with_retries
    from llm.stream_idle import STALLED_ERROR_KIND

    class NeverYields:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()

    async def create(**_kwargs):
        return NeverYields()

    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    response = await asyncio.wait_for(
        _responses_with_retries(
            client, {}, model="openai/m", messages=[], tools=None, max_tokens=None,
            use_streaming=True, on_reasoning_chunk=None, on_content_chunk=None,
            on_tool_call_chunk=None, on_response_started=None,
            retry_transient_errors=True,
        ),
        timeout=BOUND_S,
    )
    assert response.finish_reason == "error"
    assert response.error_kind == STALLED_ERROR_KIND


@pytest.mark.asyncio
async def test_stalled_call_ends_the_turn_as_a_classified_error(isolated_db, monkeypatch):
    """The real provider stack, driven by the real Native loop."""
    import llm.base as llm_base
    from core import turn_iteration
    from core.destination_fit import build_destination_fit_ledger

    stream = _Stream([_chunk("thinking about it")], tail="hang")
    monkeypatch.setattr(llm_base, "acompletion", _acompletion(stream))
    steps: list[tuple[str, dict]] = []

    async def on_step(kind, payload):
        steps.append((kind, payload))

    messages = [{"role": "user", "content": "go"}]
    sections = {
        "fixed": 0, "checkpoint": 0, "tail": 0, "current_input": 5, "tools": 0,
        "provider_wrapper": 2,
    }
    ledger = build_destination_fit_ledger(
        harness="native", target_catalog_id="openai/m", proven_target_window_tokens=1000,
        accounting_payload={"messages": messages, "tools": []},
        rendered_input_tokens=sum(sections.values()), section_tokens=sections,
        input_identity="fifo-head", current_input=messages[-1],
        components={"fixed": "", "checkpoint": "", "tail": [],
                    "current_input": messages[-1], "tools": []},
    )
    state = turn_iteration._IterationState(
        messages=messages, context_usage={}, tool_definitions=[],
        context_identity=None, fit_ledger=ledger, fit_current_input=messages[-1],
        checkpoint_source_messages=[], turn_model="openai/m",
    )
    params = {
        "model": "openai/m", "api_base": "http://provider.invalid/v1",
        "api_key": "sk-test", "uses_streaming": True,
    }
    agent = SimpleNamespace(max_iterations=5, temperature=0)
    with pytest.raises(turn_iteration.TurnProviderError, match="no data"):
        await asyncio.wait_for(
            turn_iteration._run_loop(
                agent, state, THREAD, "openai/m", params, asyncio.Event(), on_step,
                None, None, None, None, None,
            ),
            timeout=BOUND_S,
        )

    errors = [payload for kind, payload in steps if kind == "error"]
    assert len(errors) == 1 and "no data" in errors[0]["message"]
    assert stream.closed
    from infra import state as runtime_state

    assert THREAD not in runtime_state._active_llm_tasks


def test_error_terminal_frees_the_thread_for_the_queued_input():
    """Existing terminal owner: an errored Turn hands the FIFO head to a successor."""
    from infra.input_admission import accept_thread_input
    from infra.thread_driver import TerminalResult, finish_turn
    from tests.test_clean_turn_terminal import _db, _running_turn, _session

    db: sqlite3.Connection = _db()
    try:
        _session(db, THREAD)
        db.commit()
        _running_turn(db, THREAD, "stalled-turn")
        accept_thread_input(
            THREAD, role="user", content="are you there?", intent="queue_after_turn",
            message_id="queued-behind-stall", db=db,
        )
        finished = finish_turn(
            "stalled-turn", outcome="error", ended_at="t3",
            successor_turn_id="successor-turn", boot_epoch="boot-1", db=db,
        )
        assert finished.result is TerminalResult.APPLIED
        assert finished.successor is not None
        assert [tuple(row) for row in db.execute(
            "SELECT id,status FROM active_turns ORDER BY turn_order"
        )] == [("stalled-turn", "error"), ("successor-turn", "running")]
        assert db.execute(
            "SELECT active_turn_id FROM messages WHERE id='queued-behind-stall'"
        ).fetchone()[0] == "successor-turn"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# #2485 — a stream that ends before its terminal event
# ---------------------------------------------------------------------------


def _anthropic_text_event(text: str):
    return SimpleNamespace(
        type="content_block_delta", index=0,
        delta=SimpleNamespace(type="text_delta", text=text),
    )


def _anthropic_terminal_event():
    return SimpleNamespace(type="message_stop")


def _responses_text_event(text: str):
    return SimpleNamespace(type="response.output_text.delta", delta=text)


def _responses_terminal_event():
    return SimpleNamespace(
        type="response.completed",
        response=SimpleNamespace(usage=None, status="completed"),
    )


def _client(create, attr: str):
    """A provider client whose ``create`` is the given callable."""
    return SimpleNamespace(**{attr: SimpleNamespace(create=create)})


def _returning(stream):
    async def create(**_kwargs):
        return stream

    return create


def _three_loops(*, text: str = "partial", terminal: bool = False,
                 on_content=None, on_reasoning=None):
    """The same scripted provider stream, ending after its chunks, on all three loops.

    ``terminal=False`` closes the stream without the provider's terminal event.
    """
    from llm.base_streaming import streaming_completion
    from llm.providers.anthropic_streaming import _streaming_completion
    from llm.providers.openai_streaming import _streaming_responses_completion

    plain = [_chunk(text)] + ([_finished_chunk("")] if terminal else [])
    anthropic = [_anthropic_text_event(text)] + (
        [_anthropic_terminal_event()] if terminal else []
    )
    responses = [_responses_text_event(text)] + (
        [_responses_terminal_event()] if terminal else []
    )
    return [
        ("openai/m", streaming_completion(
            {"model": "openai/m"}, "openai/m", on_reasoning, on_content,
            acompletion_fn=_acompletion(_Stream(plain, tail="end")),
        )),
        ("anthropic/m", _streaming_completion(
            _client(_returning(_Stream(anthropic, tail="end")), "messages"),
            {}, model="anthropic/m", on_reasoning_chunk=on_reasoning,
            on_content_chunk=on_content, on_tool_call_chunk=None,
        )),
        ("openai/m", _streaming_responses_completion(
            _client(_returning(_Stream(responses, tail="end")), "responses"),
            {}, model="openai/m", on_reasoning_chunk=on_reasoning,
            on_content_chunk=on_content, on_tool_call_chunk=None,
        )),
    ]


@pytest.mark.asyncio
async def test_stream_with_no_terminal_event_and_no_output_is_raised():
    """#2485(a): nothing reached the Turn, so the call is retryable."""
    from llm.stream_idle import ProviderStreamTruncated

    for _model, run in _three_loops():
        with pytest.raises(ProviderStreamTruncated):
            await asyncio.wait_for(run, timeout=BOUND_S)


@pytest.mark.asyncio
async def test_stream_with_no_terminal_event_after_output_is_a_classified_error():
    """#2485(b): output already reached the Turn, so it ends as a visible error."""
    from llm.stream_idle import TRUNCATED_ERROR_KIND

    for model, run in _three_loops(on_content=_sink, on_reasoning=_sink):
        response = await asyncio.wait_for(run, timeout=BOUND_S)
        assert response.finish_reason == "error"
        assert response.error_kind == TRUNCATED_ERROR_KIND
        assert response.content == (
            f"The provider for {model} ended the response early, so the call was "
            "ended. Retry, or switch models if it persists."
        )


@pytest.mark.asyncio
async def test_stream_that_sends_its_terminal_event_is_unchanged():
    """The terminal marker still ends the call normally when the stream then closes."""
    for _model, run in _three_loops(text="done", terminal=True):
        response = await asyncio.wait_for(run, timeout=BOUND_S)
        assert (response.finish_reason, response.error_kind) == ("stop", None)
        assert response.content == "done"


@pytest.mark.asyncio
async def test_truncated_stream_is_retried_and_recovers(monkeypatch):
    first = _Stream([_chunk("partial")], tail="end")
    calls, run = _ladder(
        monkeypatch, [first, _Stream([_finished_chunk("hello")], tail="end")],
        callbacks=False,
    )
    response = await run()
    assert len(calls) == 2 and first.closed
    assert response.content == "hello" and response.finish_reason == "stop"


@pytest.mark.asyncio
async def test_truncated_streams_exhaust_the_ladder_as_a_classified_error(monkeypatch):
    from infra import constants
    from llm.stream_idle import TRUNCATED_ERROR_KIND

    calls, run = _ladder(
        monkeypatch,
        [_Stream([_chunk("partial")], tail="end")
         for _ in range(constants.LLM_MAX_RETRIES)],
        callbacks=False,
    )
    response = await run()
    assert len(calls) == constants.LLM_MAX_RETRIES
    assert response.error_kind == TRUNCATED_ERROR_KIND


@pytest.mark.asyncio
async def test_sdk_retry_ladders_treat_truncation_like_the_stall():
    """Both SDK ladders retry a truncated stream, then classify it once exhausted."""
    from infra import constants
    from llm.providers.anthropic import _messages_with_retries
    from llm.providers.openai import _responses_with_retries
    from llm.stream_idle import TRUNCATED_ERROR_KIND

    attempts = {"anthropic": 0, "responses": 0}

    async def anthropic_create(**_kwargs):
        attempts["anthropic"] += 1
        return _Stream([_anthropic_text_event("partial")], tail="end")

    async def responses_create(**_kwargs):
        attempts["responses"] += 1
        return _Stream([_responses_text_event("partial")], tail="end")

    anthropic_response = await asyncio.wait_for(
        _messages_with_retries(
            _client(anthropic_create, "messages"), {}, model="anthropic/m",
            messages=[], tools=None, max_tokens=100, use_streaming=True,
            on_reasoning_chunk=None, on_content_chunk=None,
            on_tool_call_chunk=None, on_response_started=None,
            retry_transient_errors=True,
        ),
        timeout=BOUND_S,
    )
    responses_response = await asyncio.wait_for(
        _responses_with_retries(
            _client(responses_create, "responses"), {}, model="openai/m",
            messages=[], tools=None, max_tokens=None, use_streaming=True,
            on_reasoning_chunk=None, on_content_chunk=None,
            on_tool_call_chunk=None, on_response_started=None,
            retry_transient_errors=True,
        ),
        timeout=BOUND_S,
    )
    assert attempts["anthropic"] == constants.LLM_MAX_RETRIES
    assert attempts["responses"] == constants.LLM_MAX_RETRIES
    for response in (anthropic_response, responses_response):
        assert response.finish_reason == "error"
        assert response.error_kind == TRUNCATED_ERROR_KIND


# ---------------------------------------------------------------------------
# #2485 round 2 — the litellm loop, through the real CustomStreamWrapper
# ---------------------------------------------------------------------------

LITELLM_MODEL = "openai/native-stop-test"
HTTP_BOUND_S = 20.0  # a real loopback HTTP boundary; a shared runner schedules slowly


def _sse_chunk(content: str | None, finish_reason: str | None = None) -> bytes:
    """One OpenAI-dialect SSE chunk, as an upstream provider would send it."""
    payload = {
        "id": "chatcmpl-truncated-test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "native-stop-test",
        "choices": [{
            "index": 0,
            "delta": {"content": content} if content is not None else {},
            "finish_reason": finish_reason,
        }],
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


class _UpstreamProbe:
    """A real SSE upstream: some text, then its finish chunk or just end of stream."""

    def __init__(self, finish: bool) -> None:
        self.finish = finish
        self.requests = 0

    async def completions(self, request: web.Request) -> web.StreamResponse:
        self.requests += 1
        await request.json()
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(_sse_chunk("partial"))
        if self.finish:
            await response.write(_sse_chunk(None, "stop"))
            await response.write(b"data: [DONE]\n\n")
        await response.write_eof()  # an upstream that ends without a finish reason
        return response


async def _start_upstream(finish: bool):
    """A loopback provider: no live model call, the wrapper is the subject."""
    probe = _UpstreamProbe(finish)
    app = web.Application()

    async def loaded_status(_request):
        return web.json_response({"models": [{"id": "native-stop-test", "loaded": True}]})

    app.router.add_get("/v1/models/status", loaded_status)
    app.router.add_post("/v1/chat/completions", probe.completions)
    server = TestServer(app, host="127.0.0.1", port=0)
    await server.start_server()
    return probe, server


def _upstream_params(server: TestServer) -> dict:
    return {
        "model": LITELLM_MODEL,
        "api_key": "native-stop-test-key",
        "api_base": str(server.make_url("/v1")).rstrip("/"),
    }


@pytest.mark.asyncio
async def test_real_litellm_stream_without_a_finish_chunk_is_a_classified_error(monkeypatch):
    """The wrapper synthesizes a final 'stop' chunk; the provider's own marker decides."""
    from infra import constants
    from llm.router import chat_completion
    from llm.stream_idle import TRUNCATED_ERROR_KIND

    monkeypatch.setattr(constants, "LLM_STREAM_IDLE_TIMEOUT_S", HTTP_BOUND_S)
    probe, server = await _start_upstream(finish=False)
    try:
        response = await asyncio.wait_for(
            chat_completion(
                [{"role": "user", "content": "go"}], None,
                on_content_chunk=_sink, **_upstream_params(server),
            ),
            timeout=HTTP_BOUND_S,
        )
    finally:
        await server.close()

    assert probe.requests == 1
    assert response.finish_reason == "error"
    assert response.error_kind == TRUNCATED_ERROR_KIND
    assert response.content == (
        f"The provider for {LITELLM_MODEL} ended the response early, so the call was "
        "ended. Retry, or switch models if it persists."
    )


@pytest.mark.asyncio
async def test_real_litellm_stream_without_a_finish_chunk_raises_before_any_output(monkeypatch):
    """A callback-less caller saw nothing, so the call is retryable, wrapper and all."""
    import llm.base as llm_base
    from infra import constants
    from llm.stream_idle import ProviderStreamTruncated

    monkeypatch.setattr(constants, "LLM_STREAM_IDLE_TIMEOUT_S", HTTP_BOUND_S)
    probe, server = await _start_upstream(finish=False)
    try:
        with pytest.raises(ProviderStreamTruncated):
            await asyncio.wait_for(
                llm_base._streaming_completion(
                    {**_upstream_params(server),
                     "messages": [{"role": "user", "content": "go"}]},
                    LITELLM_MODEL, None, None, None, None,
                ),
                timeout=HTTP_BOUND_S,
            )
    finally:
        await server.close()
    assert probe.requests == 1


@pytest.mark.asyncio
async def test_real_litellm_stream_with_its_finish_chunk_stays_a_clean_stop(monkeypatch):
    """Control: the upstream's own finish chunk still ends the call normally."""
    from infra import constants
    from llm.router import chat_completion

    monkeypatch.setattr(constants, "LLM_STREAM_IDLE_TIMEOUT_S", HTTP_BOUND_S)
    probe, server = await _start_upstream(finish=True)
    try:
        response = await asyncio.wait_for(
            chat_completion(
                [{"role": "user", "content": "go"}], None,
                on_content_chunk=_sink, **_upstream_params(server),
            ),
            timeout=HTTP_BOUND_S,
        )
    finally:
        await server.close()

    assert probe.requests == 1
    assert (response.finish_reason, response.error_kind) == ("stop", None)
    assert response.content == "partial"
