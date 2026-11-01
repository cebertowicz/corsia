"""Cold checkpoint admission and counting stay off the event loop.

Both steps read the database, render the frozen source, or run the tokenizer;
on the loop they stall every other thread's Turn for their full duration.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_boundaryless_external_cold_admission_runs_off_the_loop(monkeypatch):
    from subagents import harness_checkpoint_resume as bridge

    loop_thread = threading.get_ident()
    seen: list[int] = []
    admission = object()

    def build(checkpoint_id, *, context, live, identity):
        seen.append(threading.get_ident())
        return admission

    monkeypatch.setattr(bridge, "build_external_cold_admission", build)

    result = await bridge._prepare_external_checkpoint(
        "checkpoint-1",
        context=SimpleNamespace(boundary=object()),
        live=object(),
        identity=object(),
        warm_source=None,
    )

    assert result is admission
    assert seen and seen[0] != loop_thread


@pytest.mark.asyncio
async def test_native_cold_tool_count_and_fit_run_off_the_loop(monkeypatch):
    from core import fork_native as fork

    loop_thread = threading.get_ident()
    counted: list[int] = []
    fitted: list[int] = []

    def count(payload, writer):
        counted.append(threading.get_ident())
        return 7

    def fit(request, *, provider_surface_tokens, round_trip_tokens):
        fitted.append(threading.get_ident())
        assert provider_surface_tokens == 7
        assert round_trip_tokens == 16_384
        return SimpleNamespace(messages=(), first_included_seq=3)

    monkeypatch.setattr("core.fork_cold_count.cold_input_tokens", count)
    monkeypatch.setattr("core.fork_render.fit_cold_fork_input", fit)
    # A cold fork only ever advertises its one executable tool (#2116); use it
    # here too so this off-loop-threading contract stays meaningful.
    tools = [{"type": "function", "function": {"name": "continuity_edit"}}]
    agent = SimpleNamespace(tools=SimpleNamespace(get_definitions=lambda: tools))
    request = SimpleNamespace(
        warm_source=None, instruction="fold", target="writer", max_output_tokens=16_384,
    )

    from core.checkpoint_fork_kind import CHECKPOINT_KIND

    messages, sent_tools, first_seq = await fork._messages_and_tools(request, agent, CHECKPOINT_KIND)

    assert counted and counted[0] != loop_thread
    assert fitted and fitted[0] != loop_thread
    assert sent_tools == tools and first_seq == 3
    assert messages == [{"role": "system", "content": "fold"}, fork.COLD_CLOSING_TURN]
