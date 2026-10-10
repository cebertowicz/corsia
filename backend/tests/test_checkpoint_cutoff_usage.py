"""Measured checkpoint spend survives cutoff without replaying lifetime totals."""

import json

import pytest


@pytest.mark.parametrize("terminal", [False, True])
def test_cc_completed_call_ids_count_once_and_terminal_does_not_double_them(terminal):
    from core.checkpoint_cc_fork import _result

    def record(call_id, usage):
        return {"type": "assistant", "message": {
            "id": call_id, "content": "partial", "usage": usage,
        }}

    first = record("first", {"input_tokens": 100, "output_tokens": 20,
                            "cache_read_input_tokens": 300, "cache_creation_input_tokens": 200})
    second = record("second", {"input_tokens": 50, "output_tokens": 10})
    frames = [first, first, second]
    if terminal:
        frames.append({"type": "result", "usage": {
            "input_tokens": 150, "output_tokens": 30,
            "cache_read_input_tokens": 300, "cache_creation_input_tokens": 200,
        }})
    result = _result(b"\n".join(json.dumps(frame).encode() for frame in frames), truncated=True)
    assert result.usage == {
        "prompt_tokens": 150, "completion_tokens": 30,
        "cache_read_tokens": 300, "cache_creation_tokens": 200,
        "effective_input_tokens": 650, "total_tokens": 680,
    }


@pytest.mark.asyncio
async def test_codex_receipt_and_notification_share_existing_volume_and_call_identity():
    from core.checkpoint_codex_result import CodexCheckpointCapture
    from infra.provider_usage import TokenUsage
    from subagents.codex_app_server import CodexAppServerSession
    from subagents.codex_app_server_notifications import record_usage

    capture = CodexCheckpointCapture()
    session = CodexAppServerSession(binary="unused", task=None, on_event=capture.on_event)
    session._track_active_turn("checkpoint")
    usage = {
        "last": {"inputTokens": 100, "cachedInputTokens": 40, "outputTokens": 10},
        "total": {"inputTokens": 19000100, "cachedInputTokens": 5040, "outputTokens": 2010},
    }
    await record_usage(session, "checkpoint", usage)
    await record_usage(session, "checkpoint", usage)
    next_usage = {
        "last": {"inputTokens": 200, "cachedInputTokens": 100, "outputTokens": 20},
        "total": {"inputTokens": 19000300, "cachedInputTokens": 5140, "outputTokens": 2030},
    }
    await record_usage(session, "checkpoint", next_usage)
    await capture.on_event({"kind": "assistant_text", "text": "checkpoint"})
    await capture.on_event({"kind": "result", "token_volume": TokenUsage(160, 140, 0, 30)})
    result = capture.result()
    assert len(result.usage["calls"]) == 2
    assert [row["total_tokens"] for row in result.usage["calls"]] == [110, 220]
    assert [row["cache_read_tokens"] for row in result.usage["calls"]] == [40, 100]
    assert session._turn_volume.spent_readings("checkpoint") == 2


@pytest.mark.asyncio
async def test_native_completed_effect_failure_records_checkpoint_usage_once(isolated_db, monkeypatch):
    import asyncio
    from dataclasses import replace
    from types import SimpleNamespace

    from core import checkpoint_native_fork as native
    from llm.router import LLMResponse, ToolCallRequest
    from tests.plan_execution_helpers import ProbeTool, registry_of
    from tests.test_checkpoint_provider_adapters import _request, _source
    from tools.recorded_call import CompletedToolCall, CompletedToolFailure

    tool = ProbeTool(result="completed checkpoint output")
    registry = registry_of(tool)
    provider_calls, usage = [], []

    async def completion(**kwargs):
        provider_calls.append(kwargs)
        return LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id="private-call", name="continuity_edit", arguments={})],
            usage={"total_tokens": 150},
        )

    async def agent():
        return SimpleNamespace(max_iterations=2, tools=registry)

    async def execute(name, arguments):
        result = await tool.execute()
        raise CompletedToolFailure((CompletedToolCall(
            name=name, arguments=arguments, result=result, thread_id="thread-1",
        ),))

    async def paid(key, model, measured):
        usage.append(measured)

    monkeypatch.setattr(registry, "execute", execute)
    monkeypatch.setattr(native, "_get_agent", agent)
    monkeypatch.setattr(native, "_chat_completion", completion)
    monkeypatch.setattr(native, "_provider_params", lambda model: {"model": model})
    with pytest.raises(CompletedToolFailure):
        await native.invoke_checkpoint(
            replace(_request(_source(native)), on_usage=paid), asyncio.Event(),
        )

    assert tool.calls == 1
    assert len(provider_calls) == 1
    assert usage == [{"total_tokens": 150}]
