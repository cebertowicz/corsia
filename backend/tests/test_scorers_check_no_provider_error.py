from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from inspect_ai.scorer import Target, CORRECT, INCORRECT  # noqa: E402

from eval.scorers.registry.check_no_provider_error import (  # noqa: E402
    check_no_provider_error,
)

from _scorer_helpers import event, make_state  # noqa: E402


@pytest.mark.asyncio
async def test_pass_when_no_error_field_and_no_error_events():
    state = make_state(events=[
        event("text", iteration=0, content="hi"),
        event("tool_complete", iteration=0, name="x"),
    ])
    score = await check_no_provider_error()(state, Target(""))
    assert str(score.value) in ("CORRECT", "C"), score


@pytest.mark.asyncio
async def test_fail_when_response_error_present():
    state = make_state(error="provider returned 500")
    score = await check_no_provider_error()(state, Target(""))
    assert str(score.value) in ("INCORRECT", "I")
    assert "provider returned 500" in score.metadata["error"]


@pytest.mark.asyncio
async def test_fail_when_error_event_present():
    state = make_state(events=[
        event("text", iteration=0, content="partial"),
        event("error", iteration=0, message="rate limited"),
    ])
    score = await check_no_provider_error()(state, Target(""))
    assert str(score.value) in ("INCORRECT", "I")
    assert score.metadata["error_event_count"] == 1


@pytest.mark.asyncio
async def test_pass_when_error_event_kind_in_allow_list():
    state = make_state(events=[
        event("cancellation_error", iteration=0, message="user cancelled"),
    ])
    score = await check_no_provider_error(
        allow_kinds=["cancellation_error"],
    )(state, Target(""))
    assert str(score.value) in ("CORRECT", "C")


@pytest.mark.asyncio
async def test_fail_even_with_allow_kinds_if_other_error_kind_present():
    state = make_state(events=[
        event("cancellation_error", iteration=0, message="ok"),
        event("error", iteration=0, message="provider 500"),
    ])
    score = await check_no_provider_error(
        allow_kinds=["cancellation_error"],
    )(state, Target(""))
    assert str(score.value) in ("INCORRECT", "I")


@pytest.mark.asyncio
async def test_empty_response_passes_vacuously():
    state = make_state()
    score = await check_no_provider_error()(state, Target(""))
    assert str(score.value) in ("CORRECT", "C")
