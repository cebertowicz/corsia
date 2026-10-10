from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from inspect_ai.scorer import Target, CORRECT, INCORRECT  # noqa: E402

from eval.scorers.registry.check_history_contains import (  # noqa: E402
    check_history_contains,
)

from _scorer_helpers import make_state  # noqa: E402


@pytest.mark.asyncio
async def test_pass_when_substring_in_assistant_history():
    state = make_state(
        history=[
            {"role": "user", "content": "What's the weather?"},
            {"role": "assistant", "content": "It's sunny in Cambridge."},
        ],
    )
    score = await check_history_contains(
        mode="contains", value="sunny in Cambridge",
    )(state, Target(""))
    assert str(score.value) in ("CORRECT", "C"), score


@pytest.mark.asyncio
async def test_fail_when_substring_absent():
    state = make_state(
        history=[
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello back."},
        ],
    )
    score = await check_history_contains(
        mode="contains", value="weather",
    )(state, Target(""))
    assert str(score.value) in ("INCORRECT", "I")


@pytest.mark.asyncio
async def test_pass_regex():
    state = make_state(
        history=[
            {"role": "tool", "content": '{"result": 42}'},
        ],
    )
    score = await check_history_contains(
        mode="regex", value=r'"result":\s*\d+',
    )(state, Target(""))
    assert str(score.value) in ("CORRECT", "C")


@pytest.mark.asyncio
async def test_roles_filter_excludes_other_roles():
    state = make_state(
        history=[
            {"role": "tool", "content": "The tool result was foobar."},
            {"role": "assistant", "content": "OK."},
        ],
    )
    score = await check_history_contains(
        mode="contains", value="foobar", roles=["assistant"],
    )(state, Target(""))
    assert str(score.value) in ("INCORRECT", "I")


@pytest.mark.asyncio
async def test_roles_filter_matches_when_role_included():
    state = make_state(
        history=[
            {"role": "tool", "content": "The tool result was foobar."},
        ],
    )
    score = await check_history_contains(
        mode="contains", value="foobar", roles=["tool", "assistant"],
    )(state, Target(""))
    assert str(score.value) in ("CORRECT", "C")


@pytest.mark.asyncio
async def test_legacy_history_derived_from_state_input_and_content():
    state = make_state(
        content="The capital is Paris.",
        user_input="What is the capital of France?",
    )
    score = await check_history_contains(
        mode="contains", value="Paris",
    )(state, Target(""))
    assert str(score.value) in ("CORRECT", "C")


@pytest.mark.asyncio
async def test_fail_on_empty_history():
    state = make_state()
    score = await check_history_contains(
        mode="contains", value="anything",
    )(state, Target(""))
    assert str(score.value) in ("INCORRECT", "I")
    assert score.metadata["history_size"] == 0


@pytest.mark.asyncio
async def test_invalid_mode_raises():
    with pytest.raises(ValueError):
        check_history_contains(mode="bogus", value="x")


@pytest.mark.asyncio
async def test_invalid_regex_raises_at_factory_time():
    with pytest.raises(ValueError):
        check_history_contains(mode="regex", value="[unterminated")
