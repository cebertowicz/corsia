"""A cold Native fit leaves the room its first call reserves, however small the output."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from core.checkpoint_cold_admission import build_checkpoint_admission
from core.checkpoint_fork_kind import CHECKPOINT_KIND
from core.fork_types import FORK_TOOL_ROUND_TRIP_TOKENS
from history.types import (
    PORTABLE_HISTORY_FORMAT_VERSION,
    HistoryBoundary,
    PortableHistoryProjection,
    PortableMessage,
    PortableTurn,
)
from llm.resolution import ExecutionTarget
from llm.router import LLMResponse

WINDOW = 41_681  # the 32 GB local seat
THREAD = "cold-round-trip-thread"


def _admission(monkeypatch):
    monkeypatch.setattr("infra.continuity_store.active_entries", lambda *_: {})
    turns = []
    for n in range(1, 201):
        user, reply = (
            PortableMessage(
                message_id=f"m{seq}",
                seq=seq,
                turn_id=f"turn-{n}",
                role=role,
                content=f"setup step {seq}: " + "the person's tools and accounts. " * 24,
                created_at="2026-11-01T00:00:00Z",
            )
            for seq, role in ((2 * n - 1, "user"), (2 * n, "assistant"))
        )
        boundary = HistoryBoundary(THREAD, 1, reply.seq, reply.message_id)
        turns.append(PortableTurn(f"turn-{n}", "completed", (user, reply), boundary))
    projection = PortableHistoryProjection(
        format_version=PORTABLE_HISTORY_FORMAT_VERSION,
        thread_id=THREAD,
        history_epoch=1,
        start_exclusive=None,
        sealed_boundary=boundary,
        turns=tuple(turns),
        full_raw_replay_available=True,
    )
    target = ExecutionTarget(
        catalog_id="cold-round-trip-native",
        harness="native",
        provider="native",
        backend="local",
        transport_model="test-model",
        expected_reported_model=None,
        context_window=WINDOW,
        available=True,
        availability_reason=None,
        display_name="native",
        supports_reasoning=False,
    )
    return build_checkpoint_admission(
        checkpoint_id="cold-round-trip-checkpoint",
        thread_id=THREAD,
        trigger="pressure",
        source_target=target,
        writer_target=target,
        boundary=boundary,
        previous=None,
        projection=projection,
        working_dir="/work/project",
        reasoning_effort=None,
        binary=None,
        carried_tokens=0,
        db=object(),
    )


@pytest.mark.asyncio
async def test_halved_output_fit_still_passes_the_first_call(monkeypatch):
    """Carried tokens near the launch point halve the output under the first-call
    reserve; the fit then admitted input that call refused, and every send failed."""
    from core import fork_cold_count, fork_native as native

    monkeypatch.setattr(fork_cold_count, "_server_tokens", lambda text, _writer: len(text) // 4)
    request = replace(_admission(monkeypatch).provider_request, carried_tokens=30_000)
    output = native._window_output(request).max_output_tokens
    assert output < FORK_TOOL_ROUND_TRIP_TOKENS - WINDOW // 20
    tool = {"type": "function", "function": {
        "name": "continuity_edit", "parameters": {"type": "object", "properties": {}},
    }}
    writes: list[dict] = []

    async def get_agent():
        return SimpleNamespace(max_iterations=1, tools=SimpleNamespace(get_definitions=lambda: [tool]))

    async def completion(**kwargs):
        writes.append(kwargs)
        return LLMResponse(content="synthetic checkpoint", usage={})

    monkeypatch.setattr(native, "_get_agent", get_agent)
    monkeypatch.setattr(native, "_provider_params", lambda _: {"model": "test-model"})
    monkeypatch.setattr(native, "_chat_completion", completion)

    result = await native.invoke_fork(request, asyncio.Event(), kind=CHECKPOINT_KIND)

    assert result.raw_body == "synthetic checkpoint"
    sent = fork_cold_count.cold_input_tokens(writes[0]["messages"], request.target)
    sent += fork_cold_count.cold_input_tokens(writes[0]["tools"], request.target)
    assert sent <= WINDOW - output - FORK_TOOL_ROUND_TRIP_TOKENS
