"""A managed oMLX thread sends its catalog default without saving a person choice."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_new_managed_omlx_thread_sends_medium_for_an_unset_effort(monkeypatch):
    from core.turn_core import TurnIdentity
    from infra.send_dispatch import dispatch_native_ws
    from llm.resolution import ExecutionTarget

    captured: dict[str, str | None] = {}

    async def fake_execute_turn(*, reasoning_effort, start_ack, **_kwargs):
        captured["reasoning_effort"] = reasoning_effort
        start_ack.set_result(TurnIdentity("turn-managed-medium", 1))

    managed_shape = {
        "type": "effort_levels",
        "values": ["low", "medium", "xhigh"],
        "default": "medium",
        "applies_default": True,
    }
    monkeypatch.setattr(
        "llm.reasoning_config.reasoning_config_for_model",
        lambda _model: managed_shape,
    )
    monkeypatch.setattr("ws.turn.execute_turn", fake_execute_turn)

    target = ExecutionTarget(
        catalog_id="omlx@corsia-server/qwen",
        harness="native",
        provider="omlx",
        backend="network",
        transport_model="omlx@corsia-server/qwen",
        expected_reported_model=None,
        context_window=262144,
        available=True,
        availability_reason=None,
        display_name="Qwen",
        supports_reasoning=True,
    )

    await dispatch_native_ws(
        "thread-managed-medium",
        {"reasoning_effort": None},
        "hello",
        {"id": "user-1"},
        object(),
        target=target,
        turn_id="turn-managed-medium",
        attachments=[],
        is_retry=False,
        reasoning_effort=None,
    )

    assert captured["reasoning_effort"] == "medium"
