"""Native turns require and use the model carried by their own thread."""

from __future__ import annotations

import asyncio

import pytest

from core import turns


@pytest.mark.asyncio
async def test_turn_uses_its_own_model(monkeypatch):
    seen: dict[str, str] = {}

    async def run_native_iteration(_agent, *args, model, **kwargs):
        seen["model"] = model
        return 1, []

    monkeypatch.setattr(turns, "make_provider_params", lambda model: {"model": model})
    monkeypatch.setattr(turns.turn_iteration, "run_native_iteration", run_native_iteration)

    await turns.run_turn_with_steps(
        object(), "hi", "thread-1", "web", "thread-1",
        on_step=lambda *args: None, cancel_event=asyncio.Event(),
        model="mock/test-model",
    )

    assert seen["model"] == "mock/test-model"


@pytest.mark.asyncio
async def test_turn_without_its_model_is_an_invariant_violation():
    with pytest.raises(RuntimeError, match="Invariant violated") as raised:
        await turns.run_turn_with_steps(
            object(), "hi", "thread-1", "web", "thread-1",
            on_step=lambda *args: None, cancel_event=asyncio.Event(), model="",
        )

    assert "Admission holds" in str(raised.value)
