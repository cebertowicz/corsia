
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from _scorer_helpers import make_state  # noqa: E402


@pytest.mark.asyncio
class TestHitRatePerTurn:

    async def test_no_turn_attrs_yields_incorrect_with_explanation(self):
        from eval.scorers.registry.hit_rate_per_turn import hit_rate_per_turn
        scorer_cb = hit_rate_per_turn(bound=0.9)
        state = make_state()  # no turn_attrs
        target = object()
        score = await scorer_cb(state, target)
        assert score.value == "I"
        assert "no turn_attrs" in score.explanation.lower()

    async def test_post_first_turn_hit_above_bound_passes(self):
        from eval.scorers.registry.hit_rate_per_turn import hit_rate_per_turn
        scorer_cb = hit_rate_per_turn(bound=0.9)
        state = make_state(turn_attrs=[
            {"turn_index": 0, "cached_tokens": 0, "prefill_new_tokens": 500},
            {"turn_index": 1, "cached_tokens": 480, "prefill_new_tokens": 20},
            {"turn_index": 2, "cached_tokens": 490, "prefill_new_tokens": 10},
        ])
        target = object()
        score = await scorer_cb(state, target)
        assert score.value == "C", score.explanation

    async def test_post_first_turn_hit_below_bound_fails(self):
        from eval.scorers.registry.hit_rate_per_turn import hit_rate_per_turn
        scorer_cb = hit_rate_per_turn(bound=0.9)
        state = make_state(turn_attrs=[
            {"turn_index": 0, "cached_tokens": 0, "prefill_new_tokens": 500},
            {"turn_index": 1, "cached_tokens": 200, "prefill_new_tokens": 300},  # 0.4
        ])
        target = object()
        score = await scorer_cb(state, target)
        assert score.value == "I"
        assert "0.400" in score.explanation or "0.4" in score.explanation

    async def test_first_turn_cold_cache_does_not_fail(self):
        from eval.scorers.registry.hit_rate_per_turn import hit_rate_per_turn
        scorer_cb = hit_rate_per_turn(bound=0.9)
        state = make_state(turn_attrs=[
            {"turn_index": 0, "cached_tokens": 0, "prefill_new_tokens": 1000},  # 0% on turn 1 is OK
            {"turn_index": 1, "cached_tokens": 950, "prefill_new_tokens": 50},  # 95%
        ])
        target = object()
        score = await scorer_cb(state, target)
        assert score.value == "C"

    async def test_zero_total_tokens_skipped_not_failed(self):
        from eval.scorers.registry.hit_rate_per_turn import hit_rate_per_turn
        scorer_cb = hit_rate_per_turn(bound=0.9)
        state = make_state(turn_attrs=[
            {"turn_index": 0, "cached_tokens": 0, "prefill_new_tokens": 1000},
            {"turn_index": 1, "cached_tokens": 0, "prefill_new_tokens": 0},  # no signal
            {"turn_index": 2, "cached_tokens": 990, "prefill_new_tokens": 10},
        ])
        target = object()
        score = await scorer_cb(state, target)
        assert score.value == "C"
        assert "no token signal" in score.explanation or score.metadata["skips"]

    async def test_all_no_signal_yields_incorrect(self):
        from eval.scorers.registry.hit_rate_per_turn import hit_rate_per_turn
        scorer_cb = hit_rate_per_turn(bound=0.9)
        state = make_state(turn_attrs=[
            {"turn_index": 0},
            {"turn_index": 1},
        ])
        target = object()
        score = await scorer_cb(state, target)
        assert score.value == "I"

    async def test_explicit_turn_range_overrides_default(self):
        from eval.scorers.registry.hit_rate_per_turn import hit_rate_per_turn
        scorer_cb = hit_rate_per_turn(bound=0.9, turn_range=[1, 1])
        state = make_state(turn_attrs=[
            {"turn_index": 0, "cached_tokens": 0, "prefill_new_tokens": 1000},
        ])
        target = object()
        score = await scorer_cb(state, target)
        assert score.value == "I"

    async def test_usage_fallback_path(self):
        from eval.scorers.registry.hit_rate_per_turn import hit_rate_per_turn
        scorer_cb = hit_rate_per_turn(bound=0.9)
        state = make_state(turn_attrs=[
            {"turn_index": 0, "cached_tokens_usage": 0, "prompt_tokens": 1000},
            {"turn_index": 1, "cached_tokens_usage": 950, "prompt_tokens": 1000},
        ])
        target = object()
        score = await scorer_cb(state, target)
        assert score.value == "C"

    async def test_invalid_bound_raises(self):
        from eval.scorers.registry.hit_rate_per_turn import hit_rate_per_turn
        with pytest.raises(ValueError):
            hit_rate_per_turn(bound=1.5)
        with pytest.raises(ValueError):
            hit_rate_per_turn(bound=-0.1)

    async def test_metadata_records_per_turn_detail(self):
        from eval.scorers.registry.hit_rate_per_turn import hit_rate_per_turn
        scorer_cb = hit_rate_per_turn(bound=0.9)
        state = make_state(turn_attrs=[
            {"turn_index": 0, "cached_tokens": 0, "prefill_new_tokens": 500},
            {"turn_index": 1, "cached_tokens": 460, "prefill_new_tokens": 40},
        ])
        target = object()
        score = await scorer_cb(state, target)
        assert score.metadata["evaluated"][0]["turn"] == 2  # 1-based
        assert score.metadata["evaluated"][0]["cached_tokens"] == 460
