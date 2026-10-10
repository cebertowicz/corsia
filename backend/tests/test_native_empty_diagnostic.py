"""A native fold with no narrative fails naming its path, never an empty result (#2514, #2116)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from core import checkpoint_native_fork as native
from core.checkpoint_native_tools import CHECKPOINT_TOOL_NAME, InputRoom, input_fits, input_room
from core.checkpoint_types import CHECKPOINT_CONTEXT_TRUNCATED, ProviderCheckpointRejected
from llm.router import LLMResponse, ToolCallRequest
from test_checkpoint_provider_adapters import _async_value, _request, _source


def _tool_reply() -> LLMResponse:
    return LLMResponse(
        content=None, finish_reason="tool_calls",
        tool_calls=[ToolCallRequest(id="c1", name=CHECKPOINT_TOOL_NAME, arguments={})],
    )


async def _run(monkeypatch, replies, *, loop_fits=True, tool_fits=True, seen=None):
    """Run one cold fork; ``loop_fits`` and ``tool_fits`` may be verdicts in order."""
    monkeypatch.setattr(
        "tools.catalog.advertised_tool_definitions",
        lambda _registry: ({"type": "function", "function": {"name": CHECKPOINT_TOOL_NAME}},),
    )
    tool_verdicts = iter(tool_fits if isinstance(tool_fits, list) else [tool_fits] * 9)
    monkeypatch.setattr(
        "core.checkpoint_native_tools.input_fits_off_loop",
        lambda *_args: _async_value(next(tool_verdicts)),
    )
    verdicts = iter(loop_fits if isinstance(loop_fits, list) else [loop_fits] * 9)
    monkeypatch.setattr(native, "_input_fits", lambda *_args: _async_value(next(verdicts)))
    pending = iter(replies)

    async def completion(**kwargs):
        if seen is not None:
            seen.append(kwargs["messages"])
        return next(pending)

    class Tools:
        async def execute(self, *_args):
            return "ok" * 500

    agent = SimpleNamespace(max_iterations=5, tools=Tools())
    monkeypatch.setattr(native, "_get_agent", lambda: _async_value(agent))
    monkeypatch.setattr(native, "_chat_completion", completion)
    monkeypatch.setattr(native, "_provider_params", lambda _model: {"model": "wire"})
    request = _request(None)
    result = await native.invoke_checkpoint(request, asyncio.Event())
    return request, result


@pytest.mark.asyncio
async def test_empty_fold_fails_naming_loop_input_over_bound(monkeypatch):
    with pytest.raises(ProviderCheckpointRejected) as raised:
        await _run(monkeypatch, [_tool_reply()], loop_fits=[True, False])

    request = _request(None)
    bound = request.target.context_window - request.max_output_tokens
    message = str(raised.value)
    assert message.startswith(f"{CHECKPOINT_CONTEXT_TRUNCATED}: path=i_loop_input_over_bound iteration=1 ")
    assert f"bound={bound} unusable=None finish_reason=tool_calls error_kind=None" in message


@pytest.mark.asyncio
async def test_empty_fold_fails_naming_provider_context_limit(monkeypatch):
    overflow = LLMResponse(
        content=None, finish_reason="error", error_kind="provider_context_overflow",
    )
    with pytest.raises(ProviderCheckpointRejected) as raised:
        await _run(monkeypatch, [overflow])

    assert "path=ii_provider_context_limit iteration=0 " in str(raised.value)
    assert "finish_reason=error error_kind=provider_context_overflow" in str(raised.value)


@pytest.mark.asyncio
async def test_empty_fold_fails_naming_tool_results_over_bound(monkeypatch):
    with pytest.raises(ProviderCheckpointRejected) as raised:
        await _run(monkeypatch, [_tool_reply()], tool_fits=False)

    assert "path=iii_tool_results_over_bound iteration=0 " in str(raised.value)
    assert "finish_reason=tool_calls" in str(raised.value)


@pytest.mark.asyncio
async def test_oversize_tool_result_becomes_a_named_error_and_the_narrative_is_captured(monkeypatch):
    narrative = LLMResponse(content="The checkpoint.", finish_reason="stop")
    seen: list = []

    # Fits before the batch; the result does not fit; the short error does.
    _, result = await _run(
        monkeypatch, [_tool_reply(), narrative], tool_fits=[True, False, True], seen=seen,
    )

    assert result.raw_body == "The checkpoint."
    assert result.diagnostic is None
    sent = [m for m in seen[1] if m["role"] == "tool"]
    assert len(sent) == 1
    assert sent[0]["content"].startswith("Error: this result (1000 characters) is ")
    assert "was not sent; write the checkpoint now." in sent[0]["content"]
    assert "okok" not in sent[0]["content"]


@pytest.mark.asyncio
async def test_no_room_even_for_the_short_error_fails_named(monkeypatch):
    with pytest.raises(ProviderCheckpointRejected) as raised:
        await _run(monkeypatch, [_tool_reply()], tool_fits=[True, False, False])

    assert "path=iii_tool_results_over_bound iteration=0 " in str(raised.value)


@pytest.mark.asyncio
async def test_partial_narrative_still_returns_the_truncated_result(monkeypatch):
    reply = LLMResponse(
        content="Narrative so far.", finish_reason="tool_calls",
        tool_calls=[ToolCallRequest(id="c1", name=CHECKPOINT_TOOL_NAME, arguments={})],
    )

    _, result = await _run(monkeypatch, [reply], tool_fits=False)

    assert result.raw_body == "Narrative so far."
    assert result.diagnostic == CHECKPOINT_CONTEXT_TRUNCATED


@pytest.mark.asyncio
async def test_warm_fork_needs_a_tool_round_trip_of_room_before_it_is_admitted(monkeypatch):
    """A warm fork whose tool-call reply cannot fit is refused before it starts."""
    from core.checkpoint_types import CHECKPOINT_TOOL_ROUND_TRIP_TOKENS
    from core.token_estimation import TokenMeasurement

    bound = 5_000
    source = _source(native, provider_input_tokens=bound - 50, rendered_input_tokens=900)
    request = replace(
        _request(source),
        target=replace(_request(source).target, context_window=bound + 50),
        max_output_tokens=50,
    )
    monkeypatch.setattr(
        "core.token_estimation.measure_request",
        lambda *_a, **_k: TokenMeasurement(950, None, "model", "test"),
    )
    messages = [*source.prefix_messages, {"role": "user", "content": request.instruction}]

    # Counted equals the bound: it fits, but leaves no room for a tool-call reply.
    assert input_fits(request, messages, []) is True
    assert await native.warm_input_fits(request) is False
    roomy = replace(source, provider_input_tokens=bound - 50 - CHECKPOINT_TOOL_ROUND_TRIP_TOKENS)
    assert await native.warm_input_fits(replace(request, warm_source=roomy)) is True


def test_warm_source_without_a_provider_anchor_is_named_not_over_bound():
    request = _request(_source(native, provider_input_tokens=-1, rendered_input_tokens=10))
    messages = [{"role": "user", "content": "x"}]

    room = input_room(request, messages, [])

    assert room.unusable == "warm_token_fields_invalid"
    assert room.counted <= room.bound
    assert input_fits(request, messages, []) is False


def test_cold_room_carries_the_admission_count_and_bound():
    request = _request(None)

    room = input_room(request, [{"role": "user", "content": "x"}], [])

    assert isinstance(room, InputRoom) and room.unusable is None
    assert room.bound == request.target.context_window - request.max_output_tokens


@pytest.mark.asyncio
async def test_capture_warns_once_with_the_diagnostic_and_keeps_the_stored_error(monkeypatch):
    from loguru import logger

    from core import checkpoint_capture
    from core.checkpoint_types import ProviderCheckpointResult

    failures, lines = [], []

    async def terminal_failure(checkpoint_id, error):
        failures.append(str(error))
        return None

    monkeypatch.setattr(checkpoint_capture, "terminal_failure", terminal_failure)
    sink = logger.add(lambda message: lines.append(str(message)), level="WARNING")
    try:
        await checkpoint_capture.capture_and_install(
            "checkpoint-1", ProviderCheckpointResult("", None, "path=ii_provider_context_limit"),
        )
    finally:
        logger.remove(sink)

    assert failures == ["checkpoint provider produced no content"]
    assert len(lines) == 1 and "diagnostic=path=ii_provider_context_limit" in lines[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("window", (131_072, 262_144))
async def test_pressure_warm_fork_at_soft_zone_entry_is_admitted_warm(monkeypatch, window):
    """Pressure must fire with the room its own fork needs, so it stays warm (#2514)."""
    from core.checkpoint_fit import CHECKPOINT_INSTRUCTION_BUDGET_TOKENS, soft_pressure_remaining
    from core.token_estimation import TokenMeasurement

    entry = window - soft_pressure_remaining(window)
    source = _source(native, provider_input_tokens=entry, rendered_input_tokens=1_000)
    request = replace(
        _request(source), target=replace(_request(source).target, context_window=window),
    )
    # The fork adds only its instruction to the frozen prefix.
    monkeypatch.setattr(
        "core.token_estimation.measure_request",
        lambda *_a, **_k: TokenMeasurement(
            1_000 + CHECKPOINT_INSTRUCTION_BUDGET_TOKENS, None, "model", "test",
        ),
    )

    assert await native.warm_input_fits(request) is True


async def _run_warm(monkeypatch, reply_tokens):
    """Run a warm fork admitted with exactly its tool round trip of room."""
    from core.checkpoint_types import CHECKPOINT_TOOL_ROUND_TRIP_TOKENS
    from core.token_estimation import TokenMeasurement

    prefix = _source(native)
    instruction = "Write the current checkpoint."
    bound = 262_144 - 16_384
    source = replace(
        prefix, provider_input_tokens=bound - CHECKPOINT_TOOL_ROUND_TRIP_TOKENS - len(instruction),
        rendered_input_tokens=1_000,
    )

    def measure(request, **_kwargs):
        chars = sum(
            len(str(m.get("content") or "")) + len(str(m.get("reasoning_content") or ""))
            for m in request["messages"]
        )
        return TokenMeasurement(1_000 + chars - len("prefix"), None, "model", "test")

    monkeypatch.setattr("core.token_estimation.measure_request", measure)
    pending = iter(reply_tokens)

    async def completion(**_kwargs):
        return next(pending)

    class Tools:
        async def execute(self, *_args):
            return "ok"

    agent = SimpleNamespace(max_iterations=3, tools=Tools())
    monkeypatch.setattr(native, "_get_agent", lambda: _async_value(agent))
    monkeypatch.setattr(native, "_chat_completion", completion)
    monkeypatch.setattr(native, "_provider_params", lambda _model: {"model": "wire"})
    request = replace(_request(source), instruction=instruction)
    return await native.invoke_checkpoint(request, asyncio.Event())


def _bulky_tool_reply(chars: int) -> LLMResponse:
    return LLMResponse(
        content=None, finish_reason="tool_calls", reasoning_content="x" * chars,
        tool_calls=[ToolCallRequest(id="c1", name=CHECKPOINT_TOOL_NAME, arguments={})],
    )


@pytest.mark.asyncio
async def test_tool_call_reply_within_the_round_trip_reserve_finishes(monkeypatch):
    from core.checkpoint_types import CHECKPOINT_TOOL_ROUND_TRIP_TOKENS

    result = await _run_warm(
        monkeypatch,
        [_bulky_tool_reply(CHECKPOINT_TOOL_ROUND_TRIP_TOKENS - 10),
         LLMResponse(content="Done.", finish_reason="stop")],
    )

    assert result.raw_body == "Done."


@pytest.mark.asyncio
async def test_tool_call_reply_over_the_round_trip_reserve_fails_named(monkeypatch):
    from core.checkpoint_types import CHECKPOINT_TOOL_ROUND_TRIP_TOKENS

    with pytest.raises(ProviderCheckpointRejected) as raised:
        await _run_warm(monkeypatch, [_bulky_tool_reply(CHECKPOINT_TOOL_ROUND_TRIP_TOKENS + 10)])

    assert "path=iii_tool_results_over_bound iteration=0 " in str(raised.value)
