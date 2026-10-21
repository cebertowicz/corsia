"""One comparable usage receipt per checkpoint, whichever harness wrote it.

The 2026-09 checkpoint audit could not compute a per-checkpoint cache receipt
because the three writers stored three different shapes, and `input_tokens`
meant opposite things in two of them: for Codex it *included* cache reads, for
Claude Code it *excluded* them. Aggregating them naively produced cache shares
of 194% and 2341%.

`infra.provider_usage` already owns that reconciliation and hands back disjoint
counts. These tests pin every checkpoint writer to it.
"""

from __future__ import annotations

import json


def _anthropic_payload() -> dict:
    """A Claude Code `result` usage block: cache counts disjoint from input."""
    return {
        "input_tokens": 1_000,
        "cache_read_input_tokens": 8_000,
        "cache_creation_input_tokens": 500,
        "output_tokens": 200,
    }


def _codex_volume():
    """The canonical volume a Codex checkpoint already holds in hand."""
    from infra.provider_usage import TokenUsage

    return TokenUsage(
        input_uncached=1_000, cache_read=8_000, cache_write=0, output=200,
    )


def _native_ledger_rows() -> list[dict]:
    """Two native provider calls, each already a canonical ledger row."""
    from infra.provider_usage import TokenUsage

    first = TokenUsage(
        input_uncached=600, cache_read=5_000, cache_write=0, output=120,
    )
    second = TokenUsage(
        input_uncached=400, cache_read=3_000, cache_write=0, output=80,
    )
    return [first.ledger_row(), second.ledger_row()]


def _sum_calls(calls: list[dict]) -> dict:
    """Sum canonical per-call rows the way any reader can, because they are
    disjoint. This is the property that was impossible before: the columns
    mean the same thing in every entry, so addition is meaningful."""
    keys = ("prompt_tokens", "cache_read_tokens", "cache_creation_tokens",
            "completion_tokens")
    return {key: sum((call[key] or 0) for call in calls) for key in keys}


def _cache_share(receipt: dict) -> float:
    """Cache read over the whole prompt side, computed the one correct way."""
    prompt = (
        (receipt["prompt_tokens"] or 0)
        + (receipt["cache_read_tokens"] or 0)
        + (receipt["cache_creation_tokens"] or 0)
    )
    return (receipt["cache_read_tokens"] or 0) / prompt


def _cc_receipt(payload):
    """Exactly the composition `checkpoint_cc_fork` runs on a result block."""
    from infra.provider_usage import checkpoint_receipt, from_anthropic

    return checkpoint_receipt(from_anthropic(payload))


class TestOneShapeAcrossHarnesses:
    def test_claude_code_checkpoint_usage_is_canonical(self):
        receipt = _cc_receipt(_anthropic_payload())
        assert receipt == {
            "prompt_tokens": 1_000,
            "cache_read_tokens": 8_000,
            "cache_creation_tokens": 500,
            "completion_tokens": 200,
            "effective_input_tokens": 9_500,
            "total_tokens": 9_700,
        }

    def test_codex_checkpoint_usage_is_canonical(self):
        from core.checkpoint_codex_result import fork_usage_receipt

        receipt = fork_usage_receipt(_codex_volume())
        # The prompt side must stay disjoint: 1_000 uncached + 8_000 cached,
        # never 9_000 in prompt_tokens with 8_000 repeated beside it.
        assert receipt["prompt_tokens"] == 1_000
        assert receipt["cache_read_tokens"] == 8_000

    def test_native_per_call_rows_are_summable_because_they_are_disjoint(self):
        # Native keeps its per-call list (the durable shape #1464 established);
        # what matters is that each entry is canonical, so summing is valid.
        receipt = _sum_calls(_native_ledger_rows())
        assert receipt["prompt_tokens"] == 1_000
        assert receipt["cache_read_tokens"] == 8_000
        assert receipt["completion_tokens"] == 200

    def test_the_same_workload_reads_identically_on_every_harness(self):
        """The regression the audit hit: one workload, three answers."""
        from core.checkpoint_codex_result import fork_usage_receipt

        cc = _cc_receipt(_anthropic_payload())
        native = _sum_calls(_native_ledger_rows())
        codex = fork_usage_receipt(_codex_volume())

        # Codex reports no cache-creation concept, so compare the prompt side
        # that all three do measure.
        for receipt in (cc, native, codex):
            assert receipt["prompt_tokens"] == 1_000
            assert receipt["cache_read_tokens"] == 8_000

        assert abs(_cache_share(native) - _cache_share(codex)) < 1e-9
        # Every share is a real fraction. The audit saw 194% and 2341%.
        for receipt in (cc, native, codex):
            assert 0.0 <= _cache_share(receipt) <= 1.0

    def test_an_unmeasured_call_stays_unknown_rather_than_zero(self):
        """A missing count must not be stored as a measured zero."""
        assert _cc_receipt(None) is None
        assert _cc_receipt({"no_counts": True}) is None


class TestStoredReceiptIsJsonSerializable:
    def test_every_receipt_round_trips_through_the_checkpoint_column(self):
        receipt = _cc_receipt(_anthropic_payload())
        assert json.loads(json.dumps(receipt)) == receipt


class TestCanonicalReceiptFieldNames:
    """Canonical receipts expose the shared accounting fields.

    This shape assertion describes the new writer. It does not authorize
    retaining mixed history or provide a runtime legacy-format detector;
    historical usage is discarded separately at the stopped cutover.
    """

    def test_canonical_rows_carry_fields_no_legacy_shape_has(self):
        canonical = _cc_receipt(_anthropic_payload())
        legacy_codex = {
            "input_tokens": 300, "cached_input_tokens": 200, "output_tokens": 40,
        }
        legacy_cc = {
            "input_tokens": 1_000,
            "cache_read_input_tokens": 8_000,
            "cache_creation_input_tokens": 500,
            "output_tokens": 200,
        }
        marker = {"prompt_tokens", "effective_input_tokens"}
        assert marker <= set(canonical)
        assert not (marker & set(legacy_codex))
        assert not (marker & set(legacy_cc))
