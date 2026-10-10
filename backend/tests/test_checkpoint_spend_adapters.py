"""Paid observations leave each private fork before its next tool runs."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_native_reports_paid_call_before_tool_failure(monkeypatch):
    from core import checkpoint_native_fork as native
    from llm.router import LLMResponse, ToolCallRequest
    from test_checkpoint_provider_adapters import _request, _source

    observed = []

    async def usage(key, model, measured):
        observed.append((key, model, measured))

    async def completion(**_kwargs):
        return LLMResponse(content="partial", usage={"total_tokens": 150}, tool_calls=[
            ToolCallRequest(id="edit", name="continuity_edit", arguments={}),
        ])

    async def execute(*_args):
        assert len(observed) == 1
        raise RuntimeError("tool failed")

    async def agent():
        return SimpleNamespace(max_iterations=2, tools=SimpleNamespace(execute=execute))

    monkeypatch.setattr(native, "_get_agent", agent)
    monkeypatch.setattr(native, "_chat_completion", completion)
    monkeypatch.setattr(native, "_provider_params", lambda model: {"model": model})
    request = replace(_request(_source(native)), on_usage=usage)
    with pytest.raises(RuntimeError, match="tool failed"):
        await native.invoke_checkpoint(request, asyncio.Event())
    assert observed[0][2]["total_tokens"] == 150


@pytest.mark.asyncio
async def test_cc_complete_usage_precedes_tool_and_excludes_aggregate():
    from core.checkpoint_cc_io import _ToolBoundary
    from core.checkpoint_cc_context import CCCheckpointContext
    from test_checkpoint_external_adapters import _target

    observed = []

    async def usage(key, model, measured):
        observed.append((key, measured))

    boundary = _ToolBoundary(SimpleNamespace(returncode=None),
                             CCCheckpointContext(_target("claude-code"), 0), asyncio.Event(),
                             on_usage=usage, model="claude-test")
    await boundary.observe({"type": "assistant", "message": {
        "id": "response-1", "stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "edit", "name": "edit", "input": {}},
        ], "usage": {"input_tokens": 100, "output_tokens": 20},
    }})

    async def edit():
        assert observed[0][1]["total_tokens"] == 120
        return "done"

    assert await boundary.execute(edit, "edit", "edit", {}) == "done"
    await boundary.observe({"type": "result", "usage": {"input_tokens": 100, "output_tokens": 20}})
    assert len(observed) == 1


@pytest.mark.asyncio
async def test_codex_observed_calls_exclude_final_aggregate_and_old_duplicates():
    from core.checkpoint_codex_result import CodexCheckpointCapture
    from infra.provider_usage import TokenUsage

    observed = []

    async def usage(key, model, measured):
        observed.append((key, measured))

    capture = CodexCheckpointCapture(on_usage=usage, model="codex-test")
    event = {"kind": "call_usage", "turn_id": "fork", "ordinal": 1,
             "usage": TokenUsage(input_uncached=100, output=20).ledger_row()}
    await capture.on_event(event)
    await capture.on_event(event)
    await capture.on_event({"kind": "result", "token_volume": TokenUsage(input_uncached=100, output=20)})
    assert len(observed) == 1
    assert observed[0][1]["total_tokens"] == 120
