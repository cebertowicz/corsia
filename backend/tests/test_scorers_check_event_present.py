from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from inspect_ai.scorer import Target, CORRECT, INCORRECT  # noqa: E402

from eval.scorers.registry.check_event_present import check_event_present  # noqa: E402

from _scorer_helpers import event, make_state  # noqa: E402


@pytest.mark.asyncio
async def test_pass_when_event_kind_present_min_count_default():
    state = make_state(events=[
        event("text", iteration=0, content="hi"),
        event("tool_calls_start", iteration=0),
        event("tool_complete", iteration=0, name="null_probe"),
    ])
    score = await check_event_present(event_kind="tool_complete")(state, Target(""))
    assert str(score.value) in ("CORRECT", "C"), score
    assert score.metadata["count"] == 1


@pytest.mark.asyncio
async def test_fail_when_event_kind_absent():
    state = make_state(events=[event("text", iteration=0, content="hi")])
    score = await check_event_present(event_kind="tool_complete")(state, Target(""))
    assert str(score.value) in ("INCORRECT", "I"), score
    assert score.metadata["count"] == 0


@pytest.mark.asyncio
async def test_fail_when_below_min_count():
    state = make_state(events=[
        event("tool_complete", iteration=0, name="a"),
        event("tool_complete", iteration=1, name="b"),
    ])
    score = await check_event_present(event_kind="tool_complete", min_count=3)(
        state, Target(""),
    )
    assert str(score.value) in ("INCORRECT", "I")
    assert score.metadata["count"] == 2
    assert score.metadata["min_count"] == 3


@pytest.mark.asyncio
async def test_pass_when_within_min_and_max_window():
    state = make_state(events=[
        event("tool_complete", iteration=0, name="a"),
        event("tool_complete", iteration=1, name="b"),
        event("tool_complete", iteration=2, name="c"),
    ])
    score = await check_event_present(
        event_kind="tool_complete", min_count=2, max_count=4,
    )(state, Target(""))
    assert str(score.value) in ("CORRECT", "C")


@pytest.mark.asyncio
async def test_fail_when_above_max_count():
    state = make_state(events=[
        event("tool_complete", iteration=0, name="a"),
        event("tool_complete", iteration=1, name="b"),
        event("tool_complete", iteration=2, name="c"),
    ])
    score = await check_event_present(
        event_kind="tool_complete", max_count=2,
    )(state, Target(""))
    assert str(score.value) in ("INCORRECT", "I")
    assert score.metadata["count"] == 3
    assert score.metadata["max_count"] == 2


@pytest.mark.asyncio
async def test_fail_when_response_missing_events_field_and_no_legacy_fields():
    state = make_state()
    score = await check_event_present(event_kind="text")(state, Target(""))
    assert str(score.value) in ("INCORRECT", "I")


@pytest.mark.asyncio
async def test_legacy_response_derives_text_event_from_content():
    state = make_state(content="hello world", iteration_count=1)
    score = await check_event_present(event_kind="text")(state, Target(""))
    assert str(score.value) in ("CORRECT", "C")
