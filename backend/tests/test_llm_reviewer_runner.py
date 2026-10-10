from __future__ import annotations

import pytest
from unittest.mock import patch
from llm_reviewer_fixtures import (  # noqa: F401
    _disable_analytics,
    _reviewer_catalog,
    _reviewer_model_env,
)


class TestLabelModeDetection:
    def test_no_labels_returns_cc(self):
        from reviewer.on_pr_review import _detect_mode
        assert _detect_mode([]) == "cc"

    def test_unrelated_labels_returns_cc(self):
        from reviewer.on_pr_review import _detect_mode
        assert _detect_mode(["bug", "enhancement"]) == "cc"

    def test_review_local_label(self):
        from reviewer.on_pr_review import _detect_mode
        assert _detect_mode(["qa/review-local"]) == "local"

    def test_review_parallel_label(self):
        from reviewer.on_pr_review import _detect_mode
        assert _detect_mode(["qa/review-parallel"]) == "parallel"

    def test_parallel_takes_precedence(self):
        from reviewer.on_pr_review import _detect_mode
        assert _detect_mode(["qa/review-local", "qa/review-parallel"]) == "parallel"


class TestReviewerModelSelection:
    @pytest.fixture(autouse=True)
    def _model_env(self, monkeypatch):
        monkeypatch.setenv("CORSIA_REVIEWER_MODEL", "claude-opus-4-8")

    def test_dev_targeted_uses_base_model(self):
        from reviewer.reviewer_runner import _cc_model_for_base
        model, label = _cc_model_for_base("dev")
        assert model == "claude-opus-4-8"
        assert label == "claude-opus-4-8"

    def test_main_targeted_uses_1m_variant(self):
        from reviewer.reviewer_runner import _cc_model_for_base
        model, label = _cc_model_for_base("main")
        assert model == "claude-opus-4-8[1m]"
        assert "1M" in label

    def test_empty_base_uses_base_model(self):
        from reviewer.reviewer_runner import _cc_model_for_base
        model, _ = _cc_model_for_base("")
        assert model == "claude-opus-4-8"


class TestRetryLogic:
    @patch("reviewer.reviewer_runner.invoke_cc_subagent")
    @patch("reviewer.reviewer_runner.set_failure")
    @patch("reviewer.reviewer_runner.set_pending")
    @patch("reviewer.reviewer_runner.emit_product_event")
    @patch("time.sleep")
    def test_retries_then_fails(
        self, mock_sleep, mock_emit, mock_pending, mock_failure, mock_invoke,
    ):
        from reviewer.reviewer_backends import CCRateLimitError
        from reviewer.reviewer_runner import _run_cc_with_retries

        mock_invoke.side_effect = CCRateLimitError("429")

        result = _run_cc_with_retries(
            prompt="test", pr_number=1, repo="o/r", head_sha="abc123",
            working_dir="/tmp", model="claude-opus-4-8",
            source_git_dir="/tmp/repo.git",
            base_sha="b" * 40,
        )

        assert result is None
        mock_failure.assert_called_once()
        call_desc = mock_failure.call_args[0][2]
        assert "cc-unavailable-after-retries" in call_desc

    @patch("reviewer.reviewer_runner.invoke_cc_subagent")
    @patch("reviewer.reviewer_runner.set_pending")
    @patch("reviewer.reviewer_runner.emit_product_event")
    @patch("time.sleep")
    def test_succeeds_on_retry(
        self, mock_sleep, mock_emit, mock_pending, mock_invoke,
    ):
        from reviewer.reviewer_backends import CCRateLimitError
        from reviewer.reviewer_runner import _run_cc_with_retries

        mock_invoke.side_effect = [
            CCRateLimitError("429"),
            "[]",
        ]

        result = _run_cc_with_retries(
            prompt="test", pr_number=1, repo="o/r", head_sha="abc",
            working_dir="/tmp", model="claude-opus-4-8",
            source_git_dir="/tmp/repo.git",
            base_sha="b" * 40,
        )

        assert result == "[]"
        assert mock_sleep.call_count == 1

    @patch("reviewer.reviewer_runner.invoke_cc_subagent")
    def test_no_retry_on_success(self, mock_invoke):
        from reviewer.reviewer_runner import _run_cc_with_retries

        mock_invoke.return_value = "[]"

        result = _run_cc_with_retries(
            prompt="test", pr_number=1, repo="o/r", head_sha="abc",
            working_dir="/tmp", model="claude-opus-4-8",
            source_git_dir="/tmp/repo.git",
            base_sha="b" * 40,
        )

        assert result == "[]"
        assert mock_invoke.call_count == 1
