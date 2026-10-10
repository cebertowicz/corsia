from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from inspect_ai.scorer import Target, CORRECT, INCORRECT  # noqa: E402

from eval.scorers.registry.check_assistant_message_content import (  # noqa: E402
    check_assistant_message_content,
)

from _scorer_helpers import make_state  # noqa: E402


@pytest.mark.asyncio
async def test_pass_contains_substring_case_insensitive():
    state = make_state(content="Replacement RESPONSE: rephrased as a question")
    score = await check_assistant_message_content(
        mode="contains", value="replacement response",
    )(state, Target(""))
    assert str(score.value) in ("CORRECT", "C"), score


@pytest.mark.asyncio
async def test_fail_contains_substring_when_absent():
    state = make_state(content="some other response")
    score = await check_assistant_message_content(
        mode="contains", value="replacement response",
    )(state, Target(""))
    assert str(score.value) in ("INCORRECT", "I")


@pytest.mark.asyncio
async def test_pass_equals_case_sensitive():
    state = make_state(content="ok")
    score = await check_assistant_message_content(
        mode="equals", value="ok", case_sensitive=True,
    )(state, Target(""))
    assert str(score.value) in ("CORRECT", "C")


@pytest.mark.asyncio
async def test_fail_equals_case_sensitive_mismatch():
    state = make_state(content="OK")
    score = await check_assistant_message_content(
        mode="equals", value="ok", case_sensitive=True,
    )(state, Target(""))
    assert str(score.value) in ("INCORRECT", "I")


@pytest.mark.asyncio
async def test_pass_equals_case_insensitive_default():
    state = make_state(content="OK")
    score = await check_assistant_message_content(
        mode="equals", value="ok",
    )(state, Target(""))
    assert str(score.value) in ("CORRECT", "C")


@pytest.mark.asyncio
async def test_pass_regex_match():
    state = make_state(content="Status code: 200 OK")
    score = await check_assistant_message_content(
        mode="regex", value=r"Status code: \d+",
    )(state, Target(""))
    assert str(score.value) in ("CORRECT", "C")


@pytest.mark.asyncio
async def test_fail_regex_no_match():
    state = make_state(content="No status here")
    score = await check_assistant_message_content(
        mode="regex", value=r"Status code: \d+",
    )(state, Target(""))
    assert str(score.value) in ("INCORRECT", "I")


@pytest.mark.asyncio
async def test_fail_when_content_missing():
    state = make_state(content=None)
    score = await check_assistant_message_content(
        mode="contains", value="anything",
    )(state, Target(""))
    assert str(score.value) in ("INCORRECT", "I")
    assert score.metadata["empty_content"] is True


@pytest.mark.asyncio
async def test_invalid_mode_raises():
    with pytest.raises(ValueError):
        check_assistant_message_content(mode="bogus", value="x")


@pytest.mark.asyncio
async def test_invalid_regex_raises_at_factory_time():
    with pytest.raises(ValueError):
        check_assistant_message_content(mode="regex", value="[unterminated")
