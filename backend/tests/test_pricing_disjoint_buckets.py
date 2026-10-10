"""Cost is priced from disjoint token buckets, with nothing deducted twice."""

from __future__ import annotations

from unittest.mock import patch

import pytest


def _cost_for(usage: dict) -> float | None:
    """Price ``usage`` against a row with a distinct rate per bucket."""
    from llm import pricing

    rates = {
        "input_cost_per_token": 1.0,
        "output_cost_per_token": 10.0,
        "cache_creation_cost_per_token": 2.0,
        "cache_read_cost_per_token": 0.1,
    }

    class _Cursor:
        def fetchone(self):
            return rates

    class _Db:
        def execute(self, *_args, **_kwargs):
            return _Cursor()

        def close(self):
            return None

    with patch.object(pricing, "get_db", lambda: _Db()):
        return pricing.calculate_cost("some/model", usage)


def test_uncached_prompt_is_billed_even_when_cache_traffic_is_larger():
    """A cache-heavy turn still pays for its fresh prompt tokens.

    ``prompt_tokens`` is the uncached remainder, so deducting the cache buckets
    from it billed real tokens at zero whenever cache traffic exceeded the
    fresh prompt -- the normal case on a warm thread.
    """
    cost = _cost_for({
        "prompt_tokens": 100,
        "completion_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 300,
    })
    # 100 fresh @1.0 + 300 cache reads @0.1
    assert cost == pytest.approx(130.0)


def test_each_bucket_is_priced_on_its_own_rate():
    cost = _cost_for({
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "cache_creation_tokens": 20,
        "cache_read_tokens": 40,
    })
    # 10*1 + 5*10 + 20*2 + 40*0.1
    assert cost == pytest.approx(104.0)


def test_missing_buckets_remain_unknown_without_failing():
    assert _cost_for({}) is None
