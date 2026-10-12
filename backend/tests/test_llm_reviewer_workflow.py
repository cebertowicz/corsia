from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from llm_reviewer_fixtures import (  # noqa: F401
    _disable_analytics,
    _isolated_db,
    _reviewer_model_env,
    dispatch_pr_review,
)


class TestWorkflowIntegration:
    """Integration test: workflow fires on simulated webhook dispatch."""

    @pytest.fixture(autouse=True)
    def _setup(self, _isolated_db):
        """Set up isolated DB and stub the presence status write."""
        with patch(
            "reviewer.on_pr_review.set_presence", return_value=True,
        ) as presence:
            self.set_presence = presence
            yield

    @patch("reviewer.on_pr_review._review_diff")
    @patch("reviewer.reviewer_runner.invoke_cc_subagent")
    @patch("reviewer.on_pr_review.fetch_existing_review_comments")
    @patch("reviewer.on_pr_review.fetch_existing_issue_comments")
    @patch("reviewer.on_pr_review.publish_review")
    @patch("reviewer.on_pr_review.set_pending")
    @patch("reviewer.on_pr_review.set_success")
    @patch("reviewer.on_pr_review.set_failure")
    @patch("reviewer.on_pr_review.emit_product_event")
    def test_workflow_dispatches_on_pr_opened(
        self, mock_emit, mock_fail, mock_success, mock_pending,
        mock_publish, mock_fetch_issue, mock_fetch, mock_cc, mock_diff,
    ):
        """Full flow: pr_opened event -> reviewer -> findings -> comment + status."""
        mock_diff.return_value = (
            "diff --git a/backend/core/turns.py b/backend/core/turns.py\n"
            "--- a/backend/core/turns.py\n"
            "+++ b/backend/core/turns.py\n"
            "@@ -1,3 +1,4 @@\n"
            "+import os\n"
        )
        mock_cc.return_value = json.dumps([
            {
                "severity": "nit",
                "file": "backend/core/turns.py",
                "line": 1,
                "category": "style/readability",
                "description": "Unused import",
                "cited_section": None,
            }
        ])
        mock_fetch.return_value = []
        mock_fetch_issue.return_value = []

        payload = {
            "action": "opened",
            "pull_request": {
                "number": 99,
                "title": "Test PR",
                "head": {"sha": "abc123def"},
                "labels": [],
                "merged": False,
            },
            "repository": {"full_name": "owner/repo"},
        }
        dispatch_pr_review(payload)
        # CC subagent should have been invoked
        mock_cc.assert_called_once()
        # The review should have been published
        mock_publish.assert_called_once()
        # History is read once for reviewer context and again after the model
        # returns so publication never classifies against a stale snapshot.
        assert mock_fetch.call_count == 2
        assert mock_fetch_issue.call_count == 2
        # No prior comments → nothing to resolve
        assert mock_publish.call_args.kwargs["resolve_replies"] == []
        # Status should be success (no block findings)
        mock_success.assert_called_once()
        self.set_presence.assert_called_once_with("owner/repo", "abc123def", "pass")

    @patch("reviewer.on_pr_review._review_diff")
    @patch("reviewer.reviewer_runner.invoke_cc_subagent")
    @patch("reviewer.on_pr_review.fetch_existing_review_comments")
    @patch("reviewer.on_pr_review.fetch_existing_issue_comments")
    @patch("reviewer.on_pr_review.publish_review")
    @patch("reviewer.on_pr_review.set_pending")
    @patch("reviewer.on_pr_review.set_success")
    @patch("reviewer.on_pr_review.set_failure")
    @patch("reviewer.on_pr_review.emit_product_event")
    def test_block_finding_sets_failure(
        self, mock_emit, mock_fail, mock_success, mock_pending,
        mock_publish, mock_fetch_issue, mock_fetch, mock_cc, mock_diff,
    ):
        """PR with block findings sets status check to failure."""
        mock_diff.return_value = "--- a/x.py\n+++ b/x.py\n+pass\n"
        mock_cc.return_value = json.dumps([
            {
                "severity": "block",
                "file": "x.py",
                "line": 1,
                "category": "correctness/silent-error",
                "description": "Error swallowed",
                "cited_section": None,
            }
        ])
        mock_fetch.return_value = []
        mock_fetch_issue.return_value = []
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 100,
                "head": {"sha": "def456"},
                "labels": [],
            },
            "repository": {"full_name": "owner/repo"},
        }
        dispatch_pr_review(payload)

        mock_fail.assert_called_once()
        assert "1 block" in mock_fail.call_args[0][2]
        assert self.set_presence.call_args.args[2] == "block"

    @patch("reviewer.on_pr_review.set_success")
    @patch("reviewer.on_pr_review.emit_product_event")
    def test_override_label_skips_review(self, mock_emit, mock_success):
        """qa/override-reviewer label skips the review entirely."""
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 101,
                "head": {"sha": "ghi789"},
                "labels": [{"name": "qa/override-reviewer"}],
            },
            "repository": {"full_name": "owner/repo"},
        }
        dispatch_pr_review(payload)

        mock_success.assert_called_once()
        assert "override" in mock_success.call_args[0][2].lower()

    @patch("reviewer.on_pr_review._review_diff")
    @patch("reviewer.reviewer_runner.invoke_cc_subagent")
    @patch("reviewer.on_pr_review.fetch_existing_review_comments")
    @patch("reviewer.on_pr_review.fetch_existing_issue_comments")
    @patch("reviewer.on_pr_review.publish_review")
    @patch("reviewer.on_pr_review.set_pending")
    @patch("reviewer.on_pr_review.set_success")
    @patch("reviewer.on_pr_review.set_failure")
    @patch("reviewer.on_pr_review.emit_product_event")
    def test_dev_targeted_pr_runs_reviewer(
        self, mock_emit, mock_fail, mock_success, mock_pending,
        mock_publish, mock_fetch_issue, mock_fetch, mock_cc, mock_diff,
    ):
        """PR targeting `dev` runs the reviewer too — granular review happens
        on feature→dev diffs, not only at the dev→main promotion. (Heavy
        evals stay main-only, gated in their GHA workflows, not here.)"""
        mock_diff.return_value = "--- a/x.py\n+++ b/x.py\n+pass\n"
        mock_cc.return_value = json.dumps([])  # no findings
        mock_fetch.return_value = []
        mock_fetch_issue.return_value = []

        payload = {
            "action": "opened",
            "pull_request": {
                "number": 300,
                "head": {"sha": "devsha1"},
                "base": {"ref": "dev"},
                "labels": [],
            },
            "repository": {"full_name": "owner/repo"},
        }
        dispatch_pr_review(payload)

        mock_cc.assert_called_once()
        mock_success.assert_called_once()
        skipped = [c for c in mock_emit.call_args_list
                   if c[0][:2] == ("reviewer", "skipped")]
        assert skipped == []

    @patch("reviewer.on_pr_review._review_diff")
    @patch("reviewer.reviewer_runner.invoke_cc_subagent")
    @patch("reviewer.on_pr_review.fetch_existing_review_comments")
    @patch("reviewer.on_pr_review.fetch_existing_issue_comments")
    @patch("reviewer.on_pr_review.publish_review")
    @patch("reviewer.on_pr_review.set_pending")
    @patch("reviewer.on_pr_review.set_success")
    @patch("reviewer.on_pr_review.set_failure")
    @patch("reviewer.on_pr_review.emit_product_event")
    def test_main_targeted_pr_runs_reviewer(
        self, mock_emit, mock_fail, mock_success, mock_pending,
        mock_publish, mock_fetch_issue, mock_fetch, mock_cc, mock_diff,
    ):
        """PR targeting `main` (incl. dev → main hotfixes/promotions) runs the
        reviewer — as do all PRs."""
        mock_diff.return_value = "--- a/x.py\n+++ b/x.py\n+pass\n"
        mock_cc.return_value = json.dumps([])  # no findings
        mock_fetch.return_value = []
        mock_fetch_issue.return_value = []

        payload = {
            "action": "opened",
            "pull_request": {
                "number": 301,
                "head": {"sha": "mainsha1"},
                "base": {"ref": "main"},
                "labels": [],
            },
            "repository": {"full_name": "owner/repo"},
        }
        dispatch_pr_review(payload)

        mock_cc.assert_called_once()
        mock_success.assert_called_once()
        skipped = [c for c in mock_emit.call_args_list
                   if c[0][:2] == ("reviewer", "skipped")]
        assert skipped == []

    @patch("reviewer.on_pr_review._review_diff")
    @patch("reviewer.reviewer_runner.invoke_cc_subagent")
    @patch("reviewer.on_pr_review.fetch_existing_review_comments")
    @patch("reviewer.on_pr_review.fetch_existing_issue_comments")
    @patch("reviewer.on_pr_review.publish_review")
    @patch("reviewer.on_pr_review.set_pending")
    @patch("reviewer.on_pr_review.set_success")
    @patch("reviewer.on_pr_review.set_failure")
    @patch("reviewer.on_pr_review.emit_product_event")
    def test_missing_base_ref_still_runs_reviewer(
        self, mock_emit, mock_fail, mock_success, mock_pending,
        mock_publish, mock_fetch_issue, mock_fetch, mock_cc, mock_diff,
    ):
        """Malformed payload with empty/missing base.ref doesn't break the
        reviewer — it runs regardless of base branch."""
        mock_diff.return_value = "--- a/x.py\n+++ b/x.py\n+pass\n"
        mock_cc.return_value = json.dumps([])  # no findings
        mock_fetch.return_value = []
        mock_fetch_issue.return_value = []

        payload = {
            "action": "opened",
            "pull_request": {
                "number": 302,
                "head": {"sha": "nobasesha"},
                # base intentionally omitted → base_ref resolves to ""
                "labels": [],
            },
            "repository": {"full_name": "owner/repo"},
        }
        dispatch_pr_review(payload)

        # Reviewer ran regardless of base.
        mock_cc.assert_called_once()
        mock_success.assert_called_once()
        skipped = [c for c in mock_emit.call_args_list
                   if c[0][:2] == ("reviewer", "skipped")]
        assert skipped == []

    # -- Incident 2026-06-09 regressions ------------------------------------
    # An API billing error string was parsed as zero findings and posted
    # fake passing reviews on every PR for a day. These pin the loud-
    # failure contract at the workflow level.
