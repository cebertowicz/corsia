"""The canonical provider-usage contract: disjoint counts, accumulated volume."""

from __future__ import annotations

import pytest

from infra.provider_usage import (
    TokenUsage,
    from_anthropic,
    from_codex,
    from_litellm,
    from_openai,
)


class TestAnthropicIsDisjoint:
    """Anthropic ``input_tokens`` excludes both cache counts."""

    def test_three_input_counts_are_added_not_collapsed(self):
        usage = from_anthropic({
            "input_tokens": 100,
            "cache_read_input_tokens": 300,
            "cache_creation_input_tokens": 200,
            "output_tokens": 50,
        })
        assert usage == TokenUsage(
            input_uncached=100, cache_read=300, cache_write=200, output=50,
        )
        # Occupancy is the whole prompt, not the uncached remainder: this is
        # the value the pre-existing runtime tests assert, and it is correct.
        assert usage.input_total == 600
        assert usage.total == 650

    def test_absent_cache_counts_are_zero_not_missing(self):
        usage = from_anthropic({"input_tokens": 10, "output_tokens": 5})
        assert usage == TokenUsage(input_uncached=10, output=5)
        assert usage.input_total == 10

    @pytest.mark.parametrize(
        "input_tokens",
        [None, True, "100", -1, 1.5, float("nan"), float("inf")],
    )
    def test_unusable_input_count_yields_no_measurement(self, input_tokens):
        assert from_anthropic({"input_tokens": input_tokens}) is None

    def test_non_mapping_yields_no_measurement(self):
        assert from_anthropic(None) is None
        assert from_anthropic("not-a-mapping") is None


class TestCodexCachedIsASubset:
    """Codex ``inputTokens`` already contains ``cachedInputTokens``."""

    def test_cached_subset_is_split_out_not_added(self):
        usage = from_codex({
            "inputTokens": 1000,
            "cachedInputTokens": 300,
            "outputTokens": 40,
        })
        assert usage == TokenUsage(
            input_uncached=700, cache_read=300, output=40,
        )
        # The provider's own inputTokens is the window occupancy; splitting the
        # subset out must not change it.
        assert usage.input_total == 1000
        assert usage.total == 1040

    def test_cache_larger_than_input_cannot_invent_negative_usage(self):
        usage = from_codex({"inputTokens": 100, "cachedInputTokens": 999})
        assert usage.input_uncached == 0
        assert usage.cache_read == 100
        assert usage.input_total == 100

    @pytest.mark.parametrize(
        "input_tokens",
        [None, True, "100", -1, 1.5, float("nan"), float("inf")],
    )
    def test_unusable_input_count_yields_no_measurement(self, input_tokens):
        assert from_codex({"inputTokens": input_tokens}) is None


class TestOpenAICachedIsASubset:
    def test_cached_detail_is_split_out_not_added(self):
        usage = from_openai({
            "prompt_tokens": 800,
            "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 500},
        })
        assert usage == TokenUsage(
            input_uncached=300, cache_read=500, output=20,
        )
        assert usage.input_total == 800

    def test_missing_details_is_all_uncached(self):
        usage = from_openai({"prompt_tokens": 800, "completion_tokens": 20})
        assert usage == TokenUsage(input_uncached=800, output=20)


class TestCrossProviderComparability:
    """The point of the contract: one logical usage, one row shape."""

    def test_same_logical_usage_normalizes_identically(self):
        # 700 fresh prompt tokens + 300 served from cache + 40 output, told to
        # us in each provider's own convention.
        anthropic = from_anthropic({
            "input_tokens": 700,
            "cache_read_input_tokens": 300,
            "output_tokens": 40,
        })
        codex = from_codex({
            "inputTokens": 1000,
            "cachedInputTokens": 300,
            "outputTokens": 40,
        })
        openai = from_openai({
            "prompt_tokens": 1000,
            "completion_tokens": 40,
            "prompt_tokens_details": {"cached_tokens": 300},
        })
        assert anthropic == codex == openai
        assert anthropic.ledger_row() == codex.ledger_row() == openai.ledger_row()

    def test_ledger_columns_stay_disjoint(self):
        usage = TokenUsage(
            input_uncached=7, cache_read=11, cache_write=13, output=17,
        )
        row = usage.ledger_row()
        assert (
            row["prompt_tokens"]
            + row["cache_read_tokens"]
            + row["cache_creation_tokens"]
            + row["completion_tokens"]
        ) == row["total_tokens"] == usage.total


class TestVolumeIsAccumulatedNotSampled:
    def test_calls_add_into_the_turn_total(self):
        first = TokenUsage(input_uncached=100, cache_read=50, output=10)
        second = TokenUsage(input_uncached=5, cache_read=900, output=20)
        turn = TokenUsage() + first + second
        assert turn == TokenUsage(input_uncached=105, cache_read=950, output=30)
        # Sampling only the final call would have billed 925 of 1085.
        assert turn.total == 1085
        assert second.total == 925

    def test_cumulative_counter_is_differenced_across_the_turn(self):
        # Codex reports a thread-cumulative counter; the turn is the delta.
        at_turn_start = TokenUsage(input_uncached=40_000, cache_read=2_114)
        at_turn_end = TokenUsage(input_uncached=60_000, cache_read=5_701)
        assert (at_turn_end - at_turn_start) == TokenUsage(
            input_uncached=20_000, cache_read=3_587,
        )

    def test_counter_reset_cannot_produce_negative_volume(self):
        after_reset = TokenUsage(input_uncached=10)
        before_reset = TokenUsage(input_uncached=5_000, output=99)
        assert (after_reset - before_reset) == TokenUsage()

    def test_adding_a_foreign_type_is_rejected(self):
        with pytest.raises(TypeError):
            TokenUsage() + 5  # type: ignore[operator]


class TestLitellmConventionDiscrimination:
    """``from_litellm`` serves several vendors through one code path.

    The vendors disagree about whether cache counts sit inside
    ``prompt_tokens``, and litellm *mirrors* Anthropic's cache fields into
    ``prompt_tokens_details``, so both field styles carry the same positive
    number for Anthropic. Neither attribute presence nor a positive value can
    tell the conventions apart -- only vendor identity can.
    """

    @staticmethod
    def _usage(**kwargs):
        from litellm.types.utils import Usage

        return Usage(**kwargs)

    @staticmethod
    def _with_cached(usage, cached: int):
        from litellm.types.utils import PromptTokensDetailsWrapper

        usage.prompt_tokens_details = PromptTokensDetailsWrapper(
            cached_tokens=cached,
        )
        return usage

    def test_anthropic_mirrored_cache_stays_disjoint(self):
        """litellm copies cache_read into details; that must not double-count."""
        usage = self._usage(
            prompt_tokens=100, completion_tokens=50, total_tokens=150,
            cache_read_input_tokens=300, cache_creation_input_tokens=200,
        )
        # litellm itself populates details here -- assert that, so this test
        # fails if that upstream behaviour ever changes.
        assert usage.prompt_tokens_details.cached_tokens == 300

        measured = from_litellm(usage, model="claude-sonnet-4-20250514")
        assert measured == TokenUsage(
            input_uncached=100, cache_read=300, cache_write=200, output=50,
        )
        assert measured.input_total == 600

    def test_omlx_cached_subset_is_not_billed_as_uncached(self):
        """The ~90% overstatement this module exists to prevent.

        litellm populates the Anthropic-style attributes as 0 for non-Anthropic
        vendors, so a presence check sent oMLX down the disjoint branch and
        billed cached tokens at the full uncached rate.
        """
        usage = self._with_cached(
            self._usage(
                prompt_tokens=1_000, completion_tokens=40, total_tokens=1_040,
                cache_read_input_tokens=0, cache_creation_input_tokens=0,
            ),
            900,
        )
        measured = from_litellm(
            usage, model="omlx@ns-f378151a51/DeepSeek-V4-Flash-MXFP4-MLX",
        )
        assert measured == TokenUsage(
            input_uncached=100, cache_read=900, output=40,
        )
        # Occupancy still matches what the provider reported as the prompt.
        assert measured.input_total == 1_000

    def test_openai_cached_detail_is_a_subset(self):
        usage = self._with_cached(
            self._usage(
                prompt_tokens=800, completion_tokens=20, total_tokens=820,
            ),
            500,
        )
        assert from_litellm(usage, model="gpt-4o") == TokenUsage(
            input_uncached=300, cache_read=500, output=20,
        )

    def test_unknown_model_defaults_to_openai_compatible(self):
        """Local and custom endpoints implement the OpenAI convention."""
        usage = self._with_cached(
            self._usage(prompt_tokens=1_000, completion_tokens=10), 600,
        )
        assert from_litellm(usage, model="some-unregistered-local-model") == (
            TokenUsage(input_uncached=400, cache_read=600, output=10)
        )

    def test_claude_through_a_gateway_uses_the_subset_convention(self):
        """The motivating case for deciding on endpoint rather than model id.

        OpenRouter and LiteLLM proxies serve Claude while still reporting
        ``anthropic/claude-*``. They speak the OpenAI convention, so the model
        id is a trap: trusting it reinstates the ~90% overstatement.
        """
        usage = self._with_cached(
            self._usage(prompt_tokens=1_000, completion_tokens=40), 900,
        )
        measured = from_litellm(
            usage,
            model="anthropic/claude-3.5-sonnet",
            api_base="https://openrouter.ai/api/v1",
        )
        assert measured == TokenUsage(
            input_uncached=100, cache_read=900, output=40,
        )
        assert measured.input_total == 1_000

    def test_same_payload_without_api_base_is_read_as_native_anthropic(self):
        """The endpoint is what changes the reading -- nothing else moved."""
        usage = self._with_cached(
            self._usage(prompt_tokens=1_000, completion_tokens=40), 900,
        )
        assert from_litellm(usage, model="anthropic/claude-3.5-sonnet") == (
            TokenUsage(input_uncached=1_000, cache_read=900, output=40)
        )

    def test_api_base_overrides_the_model_id_in_the_discriminator(self):
        from infra.provider_usage import reports_cache_outside_prompt

        assert reports_cache_outside_prompt("claude-sonnet-4-20250514") is True
        assert reports_cache_outside_prompt(
            "claude-sonnet-4-20250514", api_base="http://127.0.0.1:8080/v1",
        ) is False
        # A local runtime is OpenAI-compatible with or without the hint.
        assert reports_cache_outside_prompt("omlx@ns-x/Model") is False

    def test_no_cache_traffic_is_read_the_same_either_way(self):
        usage = self._usage(
            prompt_tokens=500, completion_tokens=10, total_tokens=510,
        )
        expected = TokenUsage(input_uncached=500, output=10)
        assert from_litellm(usage, model="gpt-4o") == expected
        assert from_litellm(usage, model="claude-sonnet-4-20250514") == expected

    def test_cached_exceeding_prompt_is_disjoint_by_arithmetic(self):
        """A subset cannot exceed its superset, whatever the vendor guess."""
        usage = self._with_cached(
            self._usage(prompt_tokens=100, completion_tokens=5), 900,
        )
        measured = from_litellm(usage, model="gpt-4o")
        assert measured == TokenUsage(
            input_uncached=100, cache_read=900, output=5,
        )

    def test_absent_measurement_is_unmeasured_not_zero(self):
        """litellm defaults prompt_tokens to 0, and no real prompt is empty."""
        assert from_litellm(self._usage(completion_tokens=5)) is None
        assert from_litellm(None) is None


class TestCumulativeTurnVolumeRetention:
    def test_turns_that_never_terminate_do_not_accumulate_forever(self):
        """Interrupts and process exits end turns without a terminal."""
        from infra.turn_volume import CumulativeTurnVolume

        tracker = CumulativeTurnVolume()
        for index in range(CumulativeTurnVolume.MAX_TRACKED_TURNS + 50):
            tracker.observe(
                f"turn-{index}",
                total=TokenUsage(input_uncached=index + 1),
                last=TokenUsage(input_uncached=1),
            )

        assert len(tracker._baseline) <= CumulativeTurnVolume.MAX_TRACKED_TURNS
        assert len(tracker._latest) <= CumulativeTurnVolume.MAX_TRACKED_TURNS
        # The most recent turn is still measurable after eviction.
        newest = f"turn-{CumulativeTurnVolume.MAX_TRACKED_TURNS + 49}"
        assert tracker.take(newest) is not None

    def test_taking_a_turn_releases_its_entries(self):
        from infra.turn_volume import CumulativeTurnVolume

        tracker = CumulativeTurnVolume()
        tracker.observe(
            "turn-1",
            total=TokenUsage(input_uncached=100),
            last=TokenUsage(input_uncached=40),
        )
        assert tracker.take("turn-1") == TokenUsage(input_uncached=40)
        # Consumed exactly once: a repeated terminal claims nothing.
        assert tracker.take("turn-1") is None
        assert not tracker._baseline
        assert not tracker._latest

    def test_an_unmeasured_turn_yields_no_volume(self):
        from infra.turn_volume import CumulativeTurnVolume

        tracker = CumulativeTurnVolume()
        tracker.observe("turn-1", total=None, last=None)
        assert tracker.take("turn-1") is None
