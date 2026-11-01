from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from llm_reviewer_fixtures import _reviewer_catalog, reviewer_accounts  # noqa: F401

pytestmark = pytest.mark.usefixtures('reviewer_accounts')


@pytest.fixture(autouse=True)
def _reviewer_model_env(monkeypatch):
    monkeypatch.setenv("CORSIA_REVIEWER_MODEL", "claude-opus-4-8")
    monkeypatch.setenv("CORSIA_REVIEWER_FALLBACK_MODEL", "claude-sonnet-4-6")


@pytest.fixture(autouse=True)
def _exact_source_for_runner():
    with patch("reviewer.reviewer_runner.exact_review_source") as source:
        source.return_value.__enter__.return_value = (
            "EXACT REVIEW SOURCE", "/tmp/neutral-review", "/tmp/repo.git",
        )
        yield source


class TestCCFallback:
    @patch("reviewer.reviewer_runner.invoke_cc_subagent")
    @patch("reviewer.reviewer_runner.set_pending")
    @patch("reviewer.reviewer_runner.set_failure")
    @patch("reviewer.reviewer_runner.emit_product_event")
    def test_primary_unavailable_falls_back_to_opus(
        self, mock_emit, mock_failure, mock_pending, mock_invoke,
    ):
        from reviewer.reviewer_backends import CCUnavailableError
        from reviewer.reviewer_runner import run_cc_review

        mock_invoke.side_effect = [CCUnavailableError("529 overloaded"), "[]"]

        result = run_cc_review(
            diff="d", context_text="", pr_number=1, repo="o/r",
            head_sha="abc", working_dir="/tmp", base_ref="dev",
        )

        assert result == ("[]", "claude-sonnet-4-6 (fallback)")
        assert mock_invoke.call_args_list[0][1]["model"] == "claude-opus-4-8"
        assert mock_invoke.call_args_list[1][1]["model"] == "claude-sonnet-4-6"
        mock_failure.assert_not_called()
        mock_pending.assert_called_once()
        fallback_events = [
            c for c in mock_emit.call_args_list if c[0][:2] == ("reviewer", "fallback")
        ]
        assert len(fallback_events) == 1

    @patch("reviewer.reviewer_runner.invoke_cc_subagent")
    @patch("reviewer.reviewer_runner.set_pending")
    @patch("reviewer.reviewer_runner.set_failure")
    @patch("reviewer.reviewer_runner.emit_product_event")
    def test_usage_limit_falls_back_without_rate_limit_backoff(
        self, mock_emit, mock_failure, mock_pending, mock_invoke,
    ):
        from reviewer.reviewer_backends import CCUnavailableError
        from reviewer.reviewer_runner import run_cc_review

        mock_invoke.side_effect = [CCUnavailableError("usage rate limit reached"), "[]"]

        with patch("reviewer.reviewer_runner.time.sleep") as mock_sleep:
            result = run_cc_review(
                diff="d", context_text="", pr_number=8, repo="o/r",
                head_sha="uvw", working_dir="/tmp", base_ref="dev",
            )

        assert result == ("[]", "claude-sonnet-4-6 (fallback)")
        assert mock_invoke.call_count == 2
        mock_sleep.assert_not_called()
        mock_failure.assert_not_called()
        mock_pending.assert_called_once()
        fallback_events = [
            c for c in mock_emit.call_args_list if c[0][:2] == ("reviewer", "fallback")
        ]
        assert len(fallback_events) == 1

    @patch("reviewer.reviewer_runner.invoke_cc_subagent")
    @patch("reviewer.reviewer_backends.subprocess.run")
    @patch("reviewer.reviewer_runner.set_pending")
    @patch("reviewer.reviewer_runner.set_failure")
    @patch("reviewer.reviewer_runner.emit_product_event")
    def test_codex_capacity_jsonl_falls_back_on_the_same_head(
        self, mock_emit, mock_failure, mock_pending, mock_run, mock_cc, monkeypatch,
    ):
        from reviewer.reviewer_runner import run_cc_review

        head_sha = "a" * 40
        monkeypatch.setenv("CORSIA_REVIEWER_MODEL", "codex/gpt-5-codex")
        mock_run.return_value.returncode = 1
        mock_run.return_value.stdout = json.dumps({
            "type": "error",
            "error": {
                "message": "Selected model is at capacity. Please try a different model.",
            },
        })
        mock_run.return_value.stderr = ""
        mock_cc.return_value = "[]"

        result = run_cc_review(
            diff="d", context_text="", pr_number=10, repo="o/r",
            head_sha=head_sha, working_dir="/tmp", base_ref="dev",
            base_sha="b" * 40,
        )

        assert result == ("[]", "claude-sonnet-4-6 (fallback)")
        mock_failure.assert_not_called()
        mock_pending.assert_called_once_with(
            "o/r", head_sha,
            f"codex/gpt-5-codex unavailable {chr(0x2014)} retrying with claude-sonnet-4-6 (fallback)",
        )
        assert mock_cc.call_args.kwargs["head_sha"] == head_sha
        fallback_events = [
            call for call in mock_emit.call_args_list
            if call.args[:2] == ("reviewer", "fallback")
        ]
        assert len(fallback_events) == 1

    def test_rate_limit_reached_is_hard_usage_limit_not_retryable_429(self):
        from reviewer.reviewer_backends import (
            CCRateLimitError,
            CCUnavailableError,
            _classify_cc_error,
        )

        usage_limit = _classify_cc_error(
            "Claude AI usage rate limit reached. Your limit resets at 11 AM.",
            "usage limit reached",
        )
        reached_limit = _classify_cc_error(
            "Claude Code reached your limit for the week.",
            "weekly hard limit reached",
        )
        transient = _classify_cc_error(
            "HTTP 429 rate limit",
            "transient 429",
        )
        branded_transient = _classify_cc_error(
            "Claude API HTTP 429 rate limit reached",
            "branded transient 429",
        )
        reset_window_transient = _classify_cc_error(
            "Claude API HTTP 429 rate limit reached; resets in 60 seconds",
            "branded transient retry window",
        )
        absolute_reset_transient = _classify_cc_error(
            "Claude API HTTP 429 rate limit reached; resets at 11 AM",
            "branded transient absolute reset",
        )

        assert isinstance(usage_limit, CCUnavailableError)
        assert isinstance(reached_limit, CCUnavailableError)
        assert isinstance(transient, CCRateLimitError)
        assert isinstance(branded_transient, CCRateLimitError)
        assert isinstance(reset_window_transient, CCRateLimitError)
        assert isinstance(absolute_reset_transient, CCRateLimitError)

    @patch("reviewer.reviewer_runner.invoke_codex_reviewer")
    @patch("reviewer.reviewer_runner.invoke_cc_subagent")
    @patch("reviewer.reviewer_runner.exact_review_source")
    @patch("reviewer.reviewer_runner.set_pending")
    @patch("reviewer.reviewer_runner.set_failure")
    @patch("reviewer.reviewer_runner.emit_product_event")
    def test_usage_limit_uses_configured_codex_fallback_without_backoff(
        self, mock_emit, mock_failure, mock_pending, mock_source,
        mock_cc, mock_codex, monkeypatch,
    ):
        from reviewer.reviewer_backends import CCUnavailableError
        from reviewer.reviewer_runner import run_cc_review

        monkeypatch.setenv("CORSIA_REVIEWER_FALLBACK_MODEL", "codex/gpt-5-codex")
        mock_cc.side_effect = CCUnavailableError("rate limit reached")
        mock_codex.return_value = "[]"
        mock_source.return_value.__enter__.return_value = (
            "EXACT REVIEW SOURCE", "/tmp/neutral-review", "/tmp/repo.git",
        )

        with patch("reviewer.reviewer_runner.time.sleep") as mock_sleep:
            result = run_cc_review(
                diff="d", context_text="", pr_number=9, repo="o/r",
                head_sha="a" * 40, working_dir="/tmp", base_ref="dev",
                base_sha="b" * 40,
            )

        assert result == ("[]", "codex/gpt-5-codex (fallback)")
        mock_cc.assert_called_once()
        mock_codex.assert_called_once()
        assert mock_codex.call_args.kwargs["model"] == "gpt-5-codex"
        mock_sleep.assert_not_called()
        mock_failure.assert_not_called()
        mock_pending.assert_called_once()
        fallback_events = [
            c for c in mock_emit.call_args_list if c[0][:2] == ("reviewer", "fallback")
        ]
        assert len(fallback_events) == 1

    @patch("reviewer.reviewer_runner.invoke_cc_subagent")
    @patch("reviewer.reviewer_runner.set_pending")
    @patch("reviewer.reviewer_runner.set_failure")
    @patch("reviewer.reviewer_runner.emit_product_event")
    def test_main_base_falls_back_to_opus_1m(
        self, mock_emit, mock_failure, mock_pending, mock_invoke,
    ):
        from reviewer.reviewer_backends import CCUnavailableError
        from reviewer.reviewer_runner import run_cc_review

        mock_invoke.side_effect = [CCUnavailableError("outage"), "[]"]

        result = run_cc_review(
            diff="d", context_text="", pr_number=2, repo="o/r",
            head_sha="def", working_dir="/tmp", base_ref="main",
        )

        assert result == ("[]", "claude-sonnet-4-6 (1M, fallback)")
        assert mock_invoke.call_args_list[0][1]["model"] == "claude-opus-4-8[1m]"
        assert mock_invoke.call_args_list[1][1]["model"] == "claude-sonnet-4-6[1m]"

    @patch("reviewer.reviewer_runner.invoke_cc_subagent")
    @patch("reviewer.reviewer_runner.set_pending")
    @patch("reviewer.reviewer_runner.set_failure")
    @patch("reviewer.reviewer_runner.emit_product_event")
    def test_unresolvable_primary_uses_configured_fallback(
        self, mock_emit, mock_failure, mock_pending, mock_invoke, monkeypatch,
    ):
        from reviewer.reviewer_runner import run_cc_review

        monkeypatch.setenv("CORSIA_REVIEWER_MODEL", "missing-primary")
        mock_invoke.return_value = "[]"

        result = run_cc_review(
            diff="d", context_text="", pr_number=10, repo="o/r",
            head_sha="missing-primary", working_dir="/tmp", base_ref="dev",
        )

        assert result == ("[]", "claude-sonnet-4-6 (fallback)")
        mock_invoke.assert_called_once()
        mock_pending.assert_called_once()
        mock_failure.assert_not_called()
        assert any(c.args[:2] == ("reviewer", "fallback") for c in mock_emit.call_args_list)

    @patch("reviewer.reviewer_runner.invoke_cc_subagent")
    @patch("reviewer.reviewer_runner.set_pending")
    @patch("reviewer.reviewer_runner.set_failure")
    @patch("reviewer.reviewer_runner.emit_product_event")
    def test_unresolvable_primary_and_fallback_fail_with_disposition(
        self, mock_emit, mock_failure, mock_pending, mock_invoke, monkeypatch,
    ):
        from reviewer.reviewer_runner import run_cc_review

        monkeypatch.setenv("CORSIA_REVIEWER_MODEL", "missing-primary")
        monkeypatch.setenv("CORSIA_REVIEWER_FALLBACK_MODEL", "missing-fallback")

        result = run_cc_review(
            diff="d", context_text="", pr_number=11, repo="o/r",
            head_sha="missing-both", working_dir="/tmp", base_ref="dev",
        )

        assert result is None
        mock_invoke.assert_not_called()
        mock_pending.assert_called_once()
        mock_failure.assert_called_once()
        assert "model-unavailable" in mock_failure.call_args.args[2]
        assert any(c.args[:2] == ("reviewer", "failed") for c in mock_emit.call_args_list)

    @patch("reviewer.reviewer_runner.invoke_cc_subagent")
    @patch("reviewer.reviewer_runner.set_pending")
    @patch("reviewer.reviewer_runner.set_failure")
    @patch("reviewer.reviewer_runner.emit_product_event")
    def test_both_unavailable_fails_loud(
        self, mock_emit, mock_failure, mock_pending, mock_invoke,
    ):
        from reviewer.reviewer_backends import CCUnavailableError
        from reviewer.reviewer_runner import run_cc_review

        mock_invoke.side_effect = CCUnavailableError("credit balance too low")

        result = run_cc_review(
            diff="d", context_text="", pr_number=3, repo="o/r",
            head_sha="ghi", working_dir="/tmp", base_ref="dev",
        )

        assert result is None
        mock_failure.assert_called_once()
        assert "model-unavailable" in mock_failure.call_args[0][2]
        failed_events = [
            c for c in mock_emit.call_args_list if c[0][:2] == ("reviewer", "failed")
        ]
        assert len(failed_events) == 1

    @patch("reviewer.reviewer_runner.invoke_cc_subagent")
    def test_claude_primary_uses_exact_source_without_frozen_diff(self, mock_invoke):
        from reviewer.reviewer_runner import run_cc_review

        mock_invoke.return_value = "[]"

        result = run_cc_review(
            diff="FROZEN-DIFF", context_text="", pr_number=4, repo="o/r",
            head_sha="a" * 40, working_dir="/tmp", base_ref="dev",
            base_sha="b" * 40,
        )

        assert result == ("[]", "claude-opus-4-8")
        assert mock_invoke.call_count == 1
        assert mock_invoke.call_args.args[1] == "/tmp/neutral-review"
        assert mock_invoke.call_args.kwargs["source_git_dir"] == "/tmp/repo.git"
        prompt = mock_invoke.call_args.args[0]
        assert "EXACT REVIEW SOURCE" in prompt
        assert "FROZEN-DIFF" not in prompt

    @patch("reviewer.reviewer_runner.invoke_codex_reviewer")
    @patch("reviewer.reviewer_runner.invoke_cc_subagent")
    @patch("reviewer.reviewer_runner.exact_review_source")
    def test_codex_primary_uses_codex_backend(
        self, mock_source, mock_cc, mock_codex, monkeypatch,
    ):
        monkeypatch.setenv("CORSIA_REVIEWER_MODEL", "codex/codex-auto-review")
        from reviewer.reviewer_runner import run_cc_review

        mock_codex.return_value = "[]"
        mock_source.return_value.__enter__.return_value = (
            "EXACT REVIEW SOURCE", "/tmp/neutral-review", "/tmp/repo.git",
        )

        result = run_cc_review(
            diff="FROZEN-DIFF", context_text="", pr_number=7, repo="o/r",
            head_sha="a" * 40, working_dir="/tmp", base_ref="main",
            base_sha="b" * 40,
            review_history="- BLOCK [standards/eng-arch] backend/old.py",
        )

        assert result == ("[]", "codex/codex-auto-review")
        mock_codex.assert_called_once()
        assert mock_codex.call_args.kwargs["model"] == "codex-auto-review"
        assert mock_codex.call_args.args[1] == "/tmp/neutral-review"
        prompt = mock_codex.call_args.args[0]
        assert "EXACT REVIEW SOURCE" in prompt
        assert "backend/old.py" in prompt
        assert "FROZEN-DIFF" not in prompt
        mock_cc.assert_not_called()

    @patch("reviewer.reviewer_runner.invoke_codex_reviewer")
    @patch("reviewer.reviewer_runner.invoke_cc_subagent")
    def test_main_sizing_uses_resolved_harness_for_misleading_catalog_id(
        self, mock_cc, mock_codex, monkeypatch,
    ):
        monkeypatch.setenv("CORSIA_REVIEWER_MODEL", "codex/misleading-cc")
        from reviewer.reviewer_runner import run_cc_review

        mock_cc.return_value = "[]"

        result = run_cc_review(
            diff="d", context_text="", pr_number=12, repo="o/r",
            head_sha="misleading-catalog-id", working_dir="/tmp", base_ref="main",
        )

        assert result == ("[]", "codex/misleading-cc (1M)")
        mock_cc.assert_called_once()
        assert mock_cc.call_args.kwargs["model"] == "misleading-cc[1m]"
        mock_codex.assert_not_called()

    @patch("reviewer.reviewer_runner.invoke_cc_subagent")
    @patch("reviewer.reviewer_runner.set_pending")
    @patch("reviewer.reviewer_runner.set_failure")
    @patch("reviewer.reviewer_runner.emit_product_event")
    def test_no_fallback_configured_fails_without_second_attempt(
        self, mock_emit, mock_failure, mock_pending, mock_invoke, monkeypatch,
    ):
        monkeypatch.delenv("CORSIA_REVIEWER_FALLBACK_MODEL", raising=False)
        monkeypatch.setattr(
            "reviewer.reviewer_runner.get_agent_defaults", lambda: {},
        )
        from reviewer.reviewer_backends import CCUnavailableError
        from reviewer.reviewer_runner import run_cc_review

        mock_invoke.side_effect = CCUnavailableError("529 overloaded")

        result = run_cc_review(
            diff="d", context_text="", pr_number=6, repo="o/r",
            head_sha="pqr", working_dir="/tmp", base_ref="dev",
        )

        assert result is None
        assert mock_invoke.call_count == 1
        mock_pending.assert_not_called()
        mock_failure.assert_called_once()
        assert "model-unavailable" in mock_failure.call_args[0][2]
        failed = [c for c in mock_emit.call_args_list if c[0][:2] == ("reviewer", "failed")]
        assert len(failed) == 1
        assert failed[0].kwargs["properties"]["error"] == "model-unavailable-no-fallback"

    @patch("reviewer.reviewer_runner.invoke_cc_subagent")
    @patch("reviewer.reviewer_runner.set_pending")
    @patch("reviewer.reviewer_runner.set_failure")
    @patch("reviewer.reviewer_runner.emit_product_event")
    def test_fallback_prompt_carries_fallback_label(
        self, mock_emit, mock_failure, mock_pending, mock_invoke,
    ):
        from reviewer.reviewer_backends import CCUnavailableError
        from reviewer.reviewer_runner import run_cc_review

        mock_invoke.side_effect = [CCUnavailableError("outage"), "[]"]

        run_cc_review(
            diff="d", context_text="", pr_number=5, repo="o/r",
            head_sha="mno", working_dir="/tmp", base_ref="dev",
        )

        primary_prompt = mock_invoke.call_args_list[0][0][0]
        fallback_prompt = mock_invoke.call_args_list[1][0][0]
        assert "claude-opus-4-8" in primary_prompt
        assert "claude-sonnet-4-6 (fallback)" in fallback_prompt
