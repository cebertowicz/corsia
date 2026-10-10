
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from _scorer_helpers import make_state  # noqa: E402


_HASH_A = "a" * 64
_HASH_B = "b" * 64


@pytest.mark.asyncio
class TestSystemPromptStable:

    async def test_no_turn_attrs_yields_incorrect(self):
        from eval.scorers.registry.system_prompt_stable import system_prompt_stable
        scorer_cb = system_prompt_stable()
        state = make_state()
        score = await scorer_cb(state, object())
        assert score.value == "I"
        assert "no turn_attrs" in score.explanation.lower()

    async def test_identical_hashes_pass(self):
        from eval.scorers.registry.system_prompt_stable import system_prompt_stable
        scorer_cb = system_prompt_stable()
        state = make_state(turn_attrs=[
            {"turn_index": 0, "system_prompt_hash": _HASH_A},
            {"turn_index": 1, "system_prompt_hash": _HASH_A},
            {"turn_index": 2, "system_prompt_hash": _HASH_A},
        ])
        score = await scorer_cb(state, object())
        assert score.value == "C"
        assert "stable" in score.answer

    async def test_drifted_hashes_fail(self):
        from eval.scorers.registry.system_prompt_stable import system_prompt_stable
        scorer_cb = system_prompt_stable()
        state = make_state(turn_attrs=[
            {"turn_index": 0, "system_prompt_hash": _HASH_A},
            {"turn_index": 1, "system_prompt_hash": _HASH_B},
        ])
        score = await scorer_cb(state, object())
        assert score.value == "I"
        assert "drift" in score.answer.lower()
        assert "2 distinct" in score.explanation

    async def test_all_null_hashes_fails_with_instrumentation_message(self):
        from eval.scorers.registry.system_prompt_stable import system_prompt_stable
        scorer_cb = system_prompt_stable()
        state = make_state(turn_attrs=[
            {"turn_index": 0},
            {"turn_index": 1, "system_prompt_hash": None},
            {"turn_index": 2, "system_prompt_hash": ""},
        ])
        score = await scorer_cb(state, object())
        assert score.value == "I"
        assert "instrumentation" in score.explanation.lower()

    async def test_partial_capture_succeeds_when_captured_are_stable(self):
        from eval.scorers.registry.system_prompt_stable import system_prompt_stable
        scorer_cb = system_prompt_stable()
        state = make_state(turn_attrs=[
            {"turn_index": 0, "system_prompt_hash": _HASH_A},
            {"turn_index": 1},  # missing
            {"turn_index": 2, "system_prompt_hash": _HASH_A},
        ])
        score = await scorer_cb(state, object())
        assert score.value == "C"

    async def test_turn_range_restricts_evaluated_window(self):
        from eval.scorers.registry.system_prompt_stable import system_prompt_stable
        scorer_cb = system_prompt_stable(turn_range=[1, 2])
        state = make_state(turn_attrs=[
            {"turn_index": 0, "system_prompt_hash": _HASH_A},
            {"turn_index": 1, "system_prompt_hash": _HASH_A},
            {"turn_index": 2, "system_prompt_hash": _HASH_B},
        ])
        score = await scorer_cb(state, object())
        assert score.value == "C"
