from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from inspect_ai.scorer import Target, CORRECT, INCORRECT  # noqa: E402

from eval.scorers.registry.check_event_metadata_relation import (  # noqa: E402
    check_event_metadata_relation,
)

from _scorer_helpers import event, make_state  # noqa: E402


@pytest.mark.asyncio
async def test_pass_lt_relation():
    state = make_state(events=[
        event("tool_complete", iteration=0, name="a", latency_ms=20.0),
        event("tool_complete", iteration=1, name="b", latency_ms=50.0),
    ])
    score = await check_event_metadata_relation(
        event_kind="tool_complete", field="latency_ms", op="lt", value=100,
    )(state, Target(""))
    assert str(score.value) in ("CORRECT", "C"), score
    assert score.metadata["matched_count"] == 2


@pytest.mark.asyncio
async def test_fail_when_any_matched_event_violates_relation():
    state = make_state(events=[
        event("tool_complete", iteration=0, name="a", latency_ms=20.0),
        event("tool_complete", iteration=1, name="b", latency_ms=120.0),  # violates
    ])
    score = await check_event_metadata_relation(
        event_kind="tool_complete", field="latency_ms", op="lt", value=100,
    )(state, Target(""))
    assert str(score.value) in ("INCORRECT", "I")
    assert score.metadata["violation_count"] == 1


@pytest.mark.asyncio
async def test_eq_relation_string_value():
    state = make_state(events=[
        event("text", iteration=0, role="assistant", content="hi"),
    ])
    score = await check_event_metadata_relation(
        event_kind="text", field="role", op="eq", value="assistant",
    )(state, Target(""))
    assert str(score.value) in ("CORRECT", "C")


@pytest.mark.asyncio
async def test_le_ge_gt_ne_relations():
    state = make_state(events=[
        event("usage", iteration=0, tokens=50),
        event("usage", iteration=1, tokens=100),
    ])
    target = Target("")
    s_le = await check_event_metadata_relation(
        event_kind="usage", field="tokens", op="le", value=100,
    )(state, target)
    s_ge = await check_event_metadata_relation(
        event_kind="usage", field="tokens", op="ge", value=50,
    )(state, target)
    s_gt = await check_event_metadata_relation(
        event_kind="usage", field="tokens", op="gt", value=49,
    )(state, target)
    s_ne = await check_event_metadata_relation(
        event_kind="usage", field="tokens", op="ne", value=0,
    )(state, target)
    for s in (s_le, s_ge, s_gt, s_ne):
        assert str(s.value) in ("CORRECT", "C"), s


@pytest.mark.asyncio
async def test_fail_when_no_matching_event_kind():
    state = make_state(events=[event("text", iteration=0, content="hi")])
    score = await check_event_metadata_relation(
        event_kind="tool_complete", field="latency_ms", op="lt", value=100,
    )(state, Target(""))
    assert str(score.value) in ("INCORRECT", "I")
    assert score.metadata["matched_count"] == 0


@pytest.mark.asyncio
async def test_fail_when_field_missing_in_payload():
    state = make_state(events=[
        event("tool_complete", iteration=0, name="a"),  # no latency_ms
    ])
    score = await check_event_metadata_relation(
        event_kind="tool_complete", field="latency_ms", op="lt", value=100,
    )(state, Target(""))
    assert str(score.value) in ("INCORRECT", "I")
    assert score.metadata["missing_field_count"] == 1


@pytest.mark.asyncio
async def test_invalid_op_raises():
    with pytest.raises(ValueError):
        check_event_metadata_relation(
            event_kind="x", field="y", op="bogus", value=1,
        )
