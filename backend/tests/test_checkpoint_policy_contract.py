from __future__ import annotations

import pytest


def test_checkpoint_contract_covers_continuation_without_an_output_schema() -> None:
    from prompts.checkpoint import build_checkpoint_system_prompt

    lowered = build_checkpoint_system_prompt().lower()
    assert "objective and status" in lowered
    assert "constraints, approvals, and preferences" in lowered
    assert "decisions with rationale and rejected alternatives" in lowered
    assert "verification and known failures" in lowered
    assert "open questions, risks, and blockers" in lowered
    assert "the exact next action with its dependencies or ownership" in lowered
    assert "return only one json" not in lowered


@pytest.mark.parametrize(
    ("source", "prompt", "cache_read", "cache_creation", "expected"),
    [
        ("external_harness:claude-code", 2_000, 97_000, 1_000, 0.97),
        ("checkpoint:warm:claude-code", 500, 49_000, 500, 0.98),
        ("external_harness:codex", 3_000, 97_000, 0, 0.97),
        ("checkpoint:warm:codex", 1_000, 49_000, 0, 0.98),
    ],
)
def test_cache_read_fraction_uses_canonical_disjoint_semantics(
    source,
    prompt,
    cache_read,
    cache_creation,
    expected,
) -> None:
    from infra.llm_usage import cache_read_fraction

    assert cache_read_fraction(
        source=source,
        prompt_tokens=prompt,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
    ) == pytest.approx(expected)


def test_cache_read_fraction_refuses_missing_measurement() -> None:
    from infra.llm_usage import cache_read_fraction

    assert cache_read_fraction(
        source="conversation",
        prompt_tokens=100,
        cache_read_tokens=90,
        cache_creation_tokens=None,
    ) is None
