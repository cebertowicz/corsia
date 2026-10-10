
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from _scorer_helpers import make_state  # noqa: E402


@pytest.mark.asyncio
class TestTtftUnder:

    async def test_no_turn_attrs_yields_incorrect(self):
        from eval.scorers.registry.ttft_under import ttft_under
        scorer_cb = ttft_under(bound=500)
        state = make_state()
        score = await scorer_cb(state, object())
        assert score.value == "I"
        assert "no turn_attrs" in score.explanation.lower()

    async def test_all_under_bound_passes(self):
        from eval.scorers.registry.ttft_under import ttft_under
        scorer_cb = ttft_under(bound=500)
        state = make_state(turn_attrs=[
            {"turn_index": 0, "ttft_ms": 100.0},
            {"turn_index": 1, "ttft_ms": 200.0},
            {"turn_index": 2, "ttft_ms": 300.0},
        ])
        score = await scorer_cb(state, object())
        assert score.value == "C"

    async def test_one_over_bound_fails(self):
        from eval.scorers.registry.ttft_under import ttft_under
        scorer_cb = ttft_under(bound=500)
        state = make_state(turn_attrs=[
            {"turn_index": 0, "ttft_ms": 100.0},
            {"turn_index": 1, "ttft_ms": 800.0},  # over budget
        ])
        score = await scorer_cb(state, object())
        assert score.value == "I"
        assert "800" in score.explanation

    async def test_no_ttft_data_anywhere_yields_incorrect_with_streaming_hint(self):
        from eval.scorers.registry.ttft_under import ttft_under
        scorer_cb = ttft_under(bound=500)
        state = make_state(turn_attrs=[
            {"turn_index": 0},
            {"turn_index": 1},
        ])
        score = await scorer_cb(state, object())
        assert score.value == "I"
        assert "streaming" in score.explanation.lower()

    async def test_partial_ttft_capture_evaluates_present_skips_missing(self):
        from eval.scorers.registry.ttft_under import ttft_under
        scorer_cb = ttft_under(bound=500)
        state = make_state(turn_attrs=[
            {"turn_index": 0, "ttft_ms": 100.0},
            {"turn_index": 1},  # missing
            {"turn_index": 2, "ttft_ms": 200.0},
        ])
        score = await scorer_cb(state, object())
        assert score.value == "C"
        assert score.metadata["skips"]

    async def test_skip_first_turn_excludes_cold_cache_turn(self):
        from eval.scorers.registry.ttft_under import ttft_under
        scorer_cb = ttft_under(bound=500, skip_first_turn=True)
        state = make_state(turn_attrs=[
            {"turn_index": 0, "ttft_ms": 2000.0},  # cold cache, over budget
            {"turn_index": 1, "ttft_ms": 100.0},
        ])
        score = await scorer_cb(state, object())
        assert score.value == "C"

    async def test_explicit_turn_range(self):
        from eval.scorers.registry.ttft_under import ttft_under
        scorer_cb = ttft_under(bound=500, turn_range=[2, 3])
        state = make_state(turn_attrs=[
            {"turn_index": 0, "ttft_ms": 2000.0},
            {"turn_index": 1, "ttft_ms": 100.0},
            {"turn_index": 2, "ttft_ms": 200.0},
        ])
        score = await scorer_cb(state, object())
        assert score.value == "C"

    async def test_zero_or_negative_bound_raises(self):
        from eval.scorers.registry.ttft_under import ttft_under
        with pytest.raises(ValueError):
            ttft_under(bound=0)
        with pytest.raises(ValueError):
            ttft_under(bound=-1)
