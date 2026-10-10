from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CORSIA_WORKSPACE", "/tmp/corsia-test-ws")

from llm.pricing import (  # noqa: E402
    calculate_cost,
    is_external_model,
    is_historical_subscription_usage,
    is_local_model,
    is_subscription_usage,
    recorded_cost,
)


def test_is_local_model_recognizes_only_the_managed_engine() -> None:
    assert is_local_model("omlx@ns-test/Local-Model-A3B-8bit") is True
    assert is_local_model("external@ns-localhost/Model-Next-80B-A3B") is False


def test_is_external_model_recognizes_only_external_endpoints() -> None:
    assert is_external_model("external@ns-localhost/Model-Next-80B-A3B") is True
    assert is_external_model("omlx@ns-test/Local-Model-A3B-8bit") is False
    assert is_external_model("openai/gpt-5.5") is False
    assert is_external_model(None) is False


def test_external_calls_record_unknown_cost_not_zero() -> None:
    model = "external@ns-x/qwen3.8-flash"
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "cache_creation_tokens": 0,
             "cache_read_tokens": 0, "cost_usd": 0.5}
    assert calculate_cost(model, usage) is None
    assert recorded_cost(model, usage) is None
    assert recorded_cost("omlx@ns-x/m", usage) == 0.0


@pytest.mark.parametrize("model", [
    "anthropic/claude-opus-4-7",
    "openai/gpt-5.4-nano",
    "openai/gpt-5.5",
    "anthropic/claude-sonnet-4-6",
])
def test_is_local_model_rejects_cloud_models(model: str) -> None:
    assert is_local_model(model) is False


@pytest.mark.parametrize("model", [None, "", "   "])
def test_is_local_model_handles_empty_input(model: str | None) -> None:
    assert is_local_model(model) is False


@pytest.mark.parametrize("harness", ["claude-code", "codex"])
def test_resolved_subscription_harness_never_records_per_call_cost(harness: str) -> None:
    usage = {"cost_usd": 0.045}
    assert is_subscription_usage(harness) is True
    assert recorded_cost("anthropic/claude-sonnet-4-6", usage, harness=harness) is None


def test_subscription_looking_model_without_resolved_harness_is_not_guessed() -> None:
    assert is_subscription_usage(None) is False
    assert recorded_cost("codex/gpt-6.1-sol", {"cost_usd": 0.045}) == 0.045


@pytest.mark.parametrize("source", [
    "host_access_application_review",
    "conversation",
    "checkpoint",
])
def test_historical_canonical_subscription_models_ignore_source(source: str) -> None:
    assert is_historical_subscription_usage("claude-code/sonnet", source) is True
    assert is_historical_subscription_usage("codex/gpt-6.1-sol", source) is True


@pytest.mark.parametrize(("model", "source"), [
    ("anthropic/claude-sonnet-4-6", "external_harness:claude-code"),
    ("openai/gpt-6.1", "external_harness:codex"),
])
def test_historical_external_harness_source_proves_transport_alias_subscription(
    model: str, source: str,
) -> None:
    assert is_historical_subscription_usage(model, source) is True


@pytest.mark.parametrize(("model", "source"), [
    ("anthropic/claude-sonnet-4-6", "conversation"),
    ("openai/gpt-6.1", "conversation"),
    ("external@ns-foo/model", "foreign_harness:codex"),
    (None, None),
    ("", ""),
])
def test_historical_subscription_reader_requires_persisted_evidence(
    model: str | None, source: str | None,
) -> None:
    assert is_historical_subscription_usage(model, source) is False


def test_historical_subscription_reader_never_resolves_the_current_catalog(monkeypatch) -> None:
    import llm.resolution

    monkeypatch.setattr(
        llm.resolution, "resolve_execution_target",
        lambda *_args, **_kwargs: pytest.fail("historical reader consulted current catalog"),
    )
    assert is_historical_subscription_usage("openai/gpt-6.1", "conversation") is False


def test_resolved_native_harness_retains_reported_charge() -> None:
    assert recorded_cost("codex/gpt-6.1-sol", {"cost_usd": 0.045}, harness="native") == 0.045


def test_calculate_cost_does_not_apply_subscription_policy() -> None:
    assert calculate_cost("omlx@local/test", {}) == 0.0
