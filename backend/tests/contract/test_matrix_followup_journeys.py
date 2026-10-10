"""An initial answer alone cannot qualify a reusable turn-lifecycle journey."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.contract import test_i10_lifecycle_matrix as matrix


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario,first_text", [
    ("plain", "PLAIN_OK"),
    ("tool", "TOOL_OK"),
    ("pressure", "PRESSURED_HEAD_DELIVERED"),
])
@pytest.mark.parametrize("followup", ["complete", "missing_text", "missing_terminal", "error"])
async def test_journey_requires_a_completed_followup_send(
    monkeypatch, tmp_path, scenario, first_text, followup,
):
    client = SimpleNamespace(
        backend=SimpleNamespace(workspace=tmp_path),
        create_session=AsyncMock(return_value="same-thread"),
        seed_replies=AsyncMock(),
        seed_pressured_history=AsyncMock(),
        call_count=AsyncMock(return_value=2),
    )
    first = [
        {"type": "tool_start"},
        {"type": "tool_done", "result": matrix.PROBE_TEXT},
        {"type": "step_text", "content": first_text},
        {"type": "turn_complete"},
    ]
    second = []
    if followup != "missing_text":
        second.append({"type": "step_text", "content": "FOLLOWUP_OK"})
    if followup != "missing_terminal":
        second.append({"type": "turn_complete"})
    if followup == "error":
        second.append({"type": "error", "message": "follow-up failed"})
    turns = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(matrix, "_one_turn", turns)

    _evidence, violation = await matrix.SCENARIO_DRIVERS[scenario](client)

    assert (violation is None) == (followup == "complete")
    assert turns.await_count == 2
    assert all(call.args[1] == "same-thread" for call in turns.await_args_list)
    assert client.create_session.await_count == 1
    assert client.seed_replies.await_count == 2
