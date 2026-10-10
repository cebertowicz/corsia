from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from inspect_ai.scorer import Target, CORRECT, INCORRECT  # noqa: E402

from eval.scorers.registry.check_event_iteration_window import (  # noqa: E402
    check_event_iteration_window,
)

from _scorer_helpers import event, make_state  # noqa: E402


@pytest.mark.asyncio
async def test_pass_when_event_in_window():
    state = make_state(events=[
        event("tool_calls_start", iteration=2),
    ])
    score = await check_event_iteration_window(
        event_kind="tool_calls_start", iteration_min=0, iteration_max=3,
    )(state, Target(""))
    assert str(score.value) in ("CORRECT", "C"), score
    assert score.metadata["in_window_count"] == 1


@pytest.mark.asyncio
async def test_pass_inclusive_lower_bound():
    state = make_state(events=[event("x", iteration=0)])
    score = await check_event_iteration_window(
        event_kind="x", iteration_min=0, iteration_max=5,
    )(state, Target(""))
    assert str(score.value) in ("CORRECT", "C")


@pytest.mark.asyncio
async def test_pass_inclusive_upper_bound():
    state = make_state(events=[event("x", iteration=5)])
    score = await check_event_iteration_window(
        event_kind="x", iteration_min=0, iteration_max=5,
    )(state, Target(""))
    assert str(score.value) in ("CORRECT", "C")


@pytest.mark.asyncio
async def test_fail_when_all_events_outside_window():
    state = make_state(events=[
        event("x", iteration=10),
        event("x", iteration=12),
    ])
    score = await check_event_iteration_window(
        event_kind="x", iteration_min=0, iteration_max=5,
    )(state, Target(""))
    assert str(score.value) in ("INCORRECT", "I")
    assert score.metadata["in_window_count"] == 0
    assert score.metadata["out_of_window_count"] == 2


@pytest.mark.asyncio
async def test_fail_when_no_events_of_kind():
    state = make_state(events=[event("y", iteration=2)])
    score = await check_event_iteration_window(
        event_kind="x", iteration_min=0, iteration_max=5,
    )(state, Target(""))
    assert str(score.value) in ("INCORRECT", "I")
    assert score.metadata["in_window_count"] == 0


@pytest.mark.asyncio
async def test_invalid_window_raises():
    with pytest.raises(ValueError):
        check_event_iteration_window(
            event_kind="x", iteration_min=5, iteration_max=2,
        )


@pytest.mark.asyncio
async def test_negative_iteration_min_raises():
    with pytest.raises(ValueError):
        check_event_iteration_window(
            event_kind="x", iteration_min=-1, iteration_max=5,
        )


@pytest.mark.asyncio
async def test_mixed_events_some_in_window():
    state = make_state(events=[
        event("tool_complete", iteration=0, name="a"),
        event("tool_complete", iteration=4, name="b"),
        event("tool_complete", iteration=10, name="c"),
    ])
    score = await check_event_iteration_window(
        event_kind="tool_complete", iteration_min=2, iteration_max=5,
    )(state, Target(""))
    assert str(score.value) in ("CORRECT", "C")
    assert score.metadata["in_window_count"] == 1
    assert score.metadata["out_of_window_count"] == 2
