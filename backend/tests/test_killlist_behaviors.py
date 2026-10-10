"""Regression pins for the kill list: HIGH collapse-risk behaviors with no test.

Source of truth: the "kill list" section of
projects/corsia/shared/turn-state-collapse/BEHAVIOR-CENSUS.md. These are
ordinary regression tests, not contract tests: they pass against the current
delivery layer and must keep passing after the simplification.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core import turn_iteration, turn_iteration_context, turn_provider_call, turns_steps


class _FakeTools:
    def get_definitions(self):
        return []


class _CeilingAgent:
    model = "omlx@test/model"
    tools = _FakeTools()

    def __init__(self, max_iterations: int) -> None:
        self.max_iterations = max_iterations

    async def _connect_mcp(self):
        return None

    def _strip_think(self, content):
        return content


def _pin_turn_seams(monkeypatch, execute_tool_calls) -> None:
    """Pin the turn body's provider/db seams so the loop itself runs for real."""

    async def endless_tool_calls(*_args, **_kwargs):
        return (
            SimpleNamespace(
                content=None,
                finish_reason="tool_calls",
                has_tool_calls=True,
                tool_calls=[SimpleNamespace(name="bash", arguments={})],
                usage={"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
            ),
            False,
        )

    async def fake_run_in_db(_fn, *_args, **_kwargs):
        return None

    monkeypatch.setattr(
        turns_steps, "lookup_thread_project_id", AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        turn_iteration,
        "prepare_turn_context",
        AsyncMock(return_value=([{"role": "user", "content": "go"}], {})),
    )
    monkeypatch.setattr(turn_provider_call, "call_llm", endless_tool_calls)
    monkeypatch.setattr(turns_steps, "emit_reasoning", AsyncMock())
    monkeypatch.setattr(turns_steps, "execute_tool_calls", execute_tool_calls)
    monkeypatch.setattr(turn_iteration, "run_in_db", fake_run_in_db)


async def test_tool_iteration_ceiling_ends_the_turn_with_an_explanatory_message(
    monkeypatch,
):
    """Census: "Hitting the tool-iteration ceiling ends the turn with an explanatory
    assistant message, not silence".

    agent.max_iterations reached with no final text must emit the explanatory
    assistant message and complete the turn normally at terminal selection.
    """
    from core import turns

    steps: list[tuple[str, dict]] = []

    async def on_step(kind: str, payload: dict):
        steps.append((kind, payload))

    async def run_tools(_agent, _response, messages, *_args, **_kwargs):
        return messages

    _pin_turn_seams(monkeypatch, run_tools)
    await turns.run_turn_with_steps(
        _CeilingAgent(max_iterations=2),
        "keep going",
        "sess-killlist-ceiling",
        "web",
        "sess-killlist-ceiling",
        on_step,
        asyncio.Event(),
        model="omlx@test/model",
        turn_provider={"uses_streaming": False},
    )

    assert ("text", {"content": (
        "I reached the maximum number of tool call iterations (2) "
        "without completing the task. You can try breaking the task "
        "into smaller steps."
    )}) in steps
    kinds = [kind for kind, _ in steps]
    assert kinds[-1] == "turn_complete"
    assert "error" not in kinds
    assert "turn_cancelled" not in kinds


async def test_a_cancel_at_the_iteration_ceiling_stays_a_cancel(monkeypatch):
    """Census: "Hitting the tool-iteration ceiling ends the turn with an explanatory
    assistant message, not silence" — the guard in its notes: "Only emitted when
    NOT cancelled — a cancel at the ceiling stays a cancel."
    """
    from core import turns

    steps: list[tuple[str, dict]] = []
    cancel_event = asyncio.Event()

    async def on_step(kind: str, payload: dict):
        steps.append((kind, payload))

    async def cancel_during_tools(_agent, _response, messages, *_args, **_kwargs):
        cancel_event.set()
        return messages

    _pin_turn_seams(monkeypatch, cancel_during_tools)
    await turns.run_turn_with_steps(
        _CeilingAgent(max_iterations=1),
        "keep going",
        "sess-killlist-ceiling-cancel",
        "web",
        "sess-killlist-ceiling-cancel",
        on_step,
        cancel_event,
        model="omlx@test/model",
        turn_provider={"uses_streaming": False},
    )

    kinds = [kind for kind, _ in steps]
    assert kinds[-1] == "turn_cancelled"
    assert "text" not in kinds
