from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core import (
    turn_iteration,
    turn_iteration_context,
    turn_provider_call,
    turns_steps,
    turns_util,
)


@pytest.mark.asyncio
async def test_run_turn_with_steps_persists_streaming_ttft_ms(monkeypatch):
    from core import turns

    recorded_usage: list[tuple[tuple, dict]] = []

    async def fake_run_in_db(fn, *args, **kwargs):
        if fn is turns_util._log_llm_usage:
            recorded_usage.append((args, kwargs))
        return None

    async def fake_call_llm(*_args, **_kwargs):
        return (
            SimpleNamespace(
                content="done",
                finish_reason="stop",
                has_tool_calls=False,
                ttft_ms=321.7,
                usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            ),
            False,
        )

    class FakeTools:
        def get_definitions(self):
            return []

    class FakeAgent:
        model = "omlx@test/model"
        max_iterations = 1
        tools = FakeTools()

        async def _connect_mcp(self):
            return None

        def _strip_think(self, content: str):
            return content

    steps: list[tuple[str, dict]] = []

    async def on_step(event: str, payload: dict):
        steps.append((event, payload))

    monkeypatch.setattr(turns_steps, "lookup_thread_project_id", AsyncMock(return_value="proj-1"))
    monkeypatch.setattr(
        turn_iteration,
        "prepare_turn_context",
        AsyncMock(return_value=(
            [{"role": "user", "content": "hello"}],
            {
                "snapshot_id": "ctx-ttft",
                "context_snapshot": {
                    "snapshot_id": "ctx-ttft",
                    "delivery_leaves": [],
                    "source_inventory": [],
                },
                "source_artifacts": [],
            },
        )),
    )
    monkeypatch.setattr(turn_provider_call, "call_llm", fake_call_llm)
    monkeypatch.setattr(turns_steps, "emit_reasoning", AsyncMock())
    monkeypatch.setattr(turn_iteration, "run_in_db", fake_run_in_db)

    cancel_event = asyncio.Event()
    cancel_event.turn_identity = SimpleNamespace(turn_id="native-turn")
    prepared_context = SimpleNamespace(
        messages=[{"role": "user", "content": "hello"}],
        context_usage={},
        tool_definitions=[],
        context_identity=None,
        fit_ledger=None,
    )
    await turns.run_turn_with_steps(
        FakeAgent(),
        "hello",
        "sess-ttft",
        "web",
        "sess-ttft",
        on_step,
        cancel_event,
        model="omlx@test/model",
        turn_provider={"uses_streaming": False, "source_harness": "native"},
        prepared_context=prepared_context,
    )

    assert recorded_usage == [
        (
            (
                "sess-ttft",
                "omlx@test/model",
                {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            ),
            {
                "ttft_ms": 321,
                "turn_id": "native-turn",
                "usage_key": "provider-call:1",
                "binding": None,
                "reasoning_effort": None,
                "harness": "native",
            },
        )
    ]
    assert ("text", {"content": "done"}) in steps
