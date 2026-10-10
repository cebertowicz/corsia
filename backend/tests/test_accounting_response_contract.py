"""Real response parsers preserve disjoint usage and unknown dollars."""

from types import SimpleNamespace

import pytest

from infra import provider_usage
from llm import pricing


def test_anthropic_parser_counts_all_input_buckets():
    from llm.providers.anthropic_translation import _parse_usage

    usage = _parse_usage(SimpleNamespace(
        input_tokens=100, cache_read_input_tokens=300,
        cache_creation_input_tokens=200, output_tokens=50,
    ))
    assert usage["effective_input_tokens"] == 600
    assert usage["total_tokens"] == 650
    assert usage["prompt_tokens"] == 100


def test_anthropic_split_stream_events_use_the_same_totals():
    from llm.providers.anthropic_streaming import _merge_stream_usage

    started = _merge_stream_usage({}, SimpleNamespace(
        input_tokens=100, cache_read_input_tokens=300, cache_creation_input_tokens=200,
    ))
    assert started["completion_tokens"] is None
    assert started["total_tokens"] is None
    completed = _merge_stream_usage(started, SimpleNamespace(output_tokens=50))
    assert completed["prompt_tokens"] == 100
    assert completed["effective_input_tokens"] == 600
    assert completed["total_tokens"] == 650


def test_responses_nonstream_splits_cached_subset(monkeypatch):
    from llm.providers.openai_translation import _parse_responses_response

    monkeypatch.setattr(pricing, "calculate_cost", lambda *_: None)
    response = _parse_responses_response(SimpleNamespace(
        output_text="done", output=[], status="completed", usage=SimpleNamespace(
            input_tokens=600, output_tokens=50, total_tokens=650,
            input_tokens_details=SimpleNamespace(cached_tokens=500),
        ),
    ), "openai/test")
    assert response.usage["prompt_tokens"] == 100
    assert response.usage["cache_read_tokens"] == 500
    assert response.usage["effective_input_tokens"] == 600
    assert response.usage["total_tokens"] == 650


@pytest.mark.parametrize("provider", ["anthropic", "litellm", "openai"])
def test_missing_output_is_unknown(provider):
    if provider == "anthropic":
        measured = provider_usage.from_anthropic({"input_tokens": 100})
    elif provider == "openai":
        measured = provider_usage.from_openai({"prompt_tokens": 100})
    else:
        measured = provider_usage.from_litellm(SimpleNamespace(prompt_tokens=100))
    assert measured is not None
    assert measured.ledger_row()["completion_tokens"] is None
    assert measured.ledger_row()["total_tokens"] is None
    assert measured.input_total == 100


def test_native_fresh_prefill_does_not_subtract_cache_twice():
    from core.turn_provider_call import _response_span_attributes

    attributes = _response_span_attributes(SimpleNamespace(
        usage={"prompt_tokens": 100, "cache_read_tokens": 500},
        has_tool_calls=False, finish_reason="stop",
    ))
    assert attributes["llm.prefill_new_tokens"] == 100


@pytest.fixture
def rates(monkeypatch):
    class PricingDB:
        def execute(self, *_args):
            return self

        def fetchone(self):
            return dict(input_cost_per_token=0.01, output_cost_per_token=0.02,
                        cache_creation_cost_per_token=0.03, cache_read_cost_per_token=0.001)

        def close(self):
            pass

    monkeypatch.setattr(pricing, "get_db", PricingDB)


def test_unknown_pricing_remains_unknown(monkeypatch):
    monkeypatch.setattr(pricing, "get_db", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    assert pricing.calculate_cost("openai/test", {}) is None


def test_local_intentional_zero_does_not_need_pricing(monkeypatch):
    monkeypatch.setattr(pricing, "get_db", lambda: pytest.fail("local model must not query cloud rates"))
    assert pricing.calculate_cost("omlx@local/test", {}) == 0.0


@pytest.mark.parametrize("missing", ["prompt_tokens", "completion_tokens", "cache_creation_tokens", "cache_read_tokens"])
def test_incomplete_measurement_cannot_be_billed_as_zero(rates, missing):
    usage = dict(prompt_tokens=100, completion_tokens=50, cache_creation_tokens=0, cache_read_tokens=500)
    usage[missing] = None
    assert pricing.calculate_cost("openai/test", usage) is None


def test_disjoint_billing_counts_each_bucket_once(rates):
    assert pricing.calculate_cost("openai/test", dict(
        prompt_tokens=100, completion_tokens=50, cache_creation_tokens=200, cache_read_tokens=300,
    )) == 8.3


def test_local_transport_alias_does_not_turn_intentional_zero_into_unknown(isolated_db):
    from infra.llm_usage import record_llm_usage
    from infra.database import get_db

    record_llm_usage("thread", "omlx@server/model", {
        "model": "openai/model", "total_tokens": 150, "cost_usd": None,
    })
    db = get_db()
    try:
        assert db.execute("SELECT cost_usd FROM llm_usage").fetchone()[0] == 0.0
    finally:
        db.close()


def test_live_cache_fraction_uses_the_same_disjoint_ledger_shape():
    from infra.llm_usage_events import cache_read_fraction

    assert cache_read_fraction(source="external_harness:codex", prompt_tokens=100,
                               cache_read_tokens=300, cache_creation_tokens=0) == 0.75
