from __future__ import annotations

import contextlib
import json
from unittest.mock import patch

import pytest

from llm_reviewer_fixtures import (  # noqa: F401
    _disable_analytics,
    _isolated_db,
    _reviewer_model_env,
    dispatch_pr_review,
)

pytestmark = pytest.mark.usefixtures("signed_in_defaults")  # a harness turn needs a signed-in Default: no ambient login


class TestWorkflowFailureOutcomes:
    """Integration test: workflow fires on simulated webhook dispatch."""

    @pytest.fixture(autouse=True)
    def _setup(self, _isolated_db):
        """Set up isolated DB for reviewer gate tests."""
        yield

    @patch("reviewer.on_pr_review._review_diff")
    @patch("reviewer.reviewer_runner.invoke_cc_subagent")
    @patch("reviewer.on_pr_review.fetch_existing_review_comments")
    @patch("reviewer.on_pr_review.fetch_existing_issue_comments")
    @patch("reviewer.on_pr_review.publish_review")
    @patch("reviewer.on_pr_review.set_pending")
    @patch("reviewer.on_pr_review.set_success")
    @patch("reviewer.on_pr_review.set_error")
    @patch("reviewer.on_pr_review.set_failure")
    @patch("reviewer.on_pr_review.emit_product_event")
    def test_billing_error_string_never_passes(
        self, mock_emit, mock_fail, mock_error, mock_success, mock_pending,
        mock_publish, mock_fetch_issue, mock_fetch, mock_cc,
        mock_diff,
    ):
        """If an error string ever flows through as 'review output', the
        check goes red — no zero-findings comment, no success status."""
        mock_diff.return_value = "--- a/x.py\n+++ b/x.py\n+pass\n"
        mock_cc.return_value = "Credit balance is too low"
        mock_fetch.return_value = []
        mock_fetch_issue.return_value = []
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 400,
                "head": {"sha": "incident1"},
                "base": {"ref": "dev"},
                "labels": [],
            },
            "repository": {"full_name": "owner/repo"},
        }
        # The workflow may re-raise after setting the error status; the
        # contract under test is the GitHub-visible outcome, not the
        # engine's propagation policy.
        with contextlib.suppress(Exception):
            dispatch_pr_review(payload)

        mock_success.assert_not_called()
        mock_publish.assert_not_called()
        assert mock_error.called or mock_fail.called

    @patch("reviewer.on_pr_review._review_diff")
    @patch("reviewer.reviewer_runner.invoke_cc_subagent")
    @patch("reviewer.reviewer_runner.set_pending")
    @patch("reviewer.reviewer_runner.set_failure")
    @patch("reviewer.reviewer_runner.emit_product_event")
    @patch("reviewer.on_pr_review.fetch_existing_review_comments")
    @patch("reviewer.on_pr_review.fetch_existing_issue_comments")
    @patch("reviewer.on_pr_review.publish_review")
    @patch("reviewer.on_pr_review.set_pending")
    @patch("reviewer.on_pr_review.set_success")
    @patch("reviewer.on_pr_review.set_failure")
    @patch("reviewer.on_pr_review.emit_product_event")
    def test_model_unavailable_fails_check(
        self, mock_emit, mock_fail, mock_success, mock_pending,
        mock_publish, mock_fetch_issue, mock_fetch,
        mock_runner_emit, mock_runner_fail, mock_runner_pending, mock_cc,
        mock_diff,
    ):
        """Primary AND fallback unavailable → qa/llm-reviewer FAILS;
        nothing is posted, success is never set."""
        from reviewer.reviewer_backends import CCUnavailableError

        mock_diff.return_value = "--- a/x.py\n+++ b/x.py\n+pass\n"
        mock_cc.side_effect = CCUnavailableError("Credit balance is too low")
        mock_fetch.return_value = []
        mock_fetch_issue.return_value = []
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 401,
                "head": {"sha": "incident2"},
                "base": {"ref": "dev"},
                "labels": [],
            },
            "repository": {"full_name": "owner/repo"},
        }
        with contextlib.suppress(Exception):
            dispatch_pr_review(payload)

        # Primary + fallback each attempted once
        assert mock_cc.call_count == 2
        mock_runner_fail.assert_called_once()
        assert "model-unavailable" in mock_runner_fail.call_args[0][2]
        mock_success.assert_not_called()
        mock_publish.assert_not_called()

    # -- Gate policy: only block_count gates the check ----------------------
    # An earlier design also gated on flag_count; this reverses it on measured
    # evidence:
    # PR #1190 ran 15+ review rounds with ZERO block findings and never went
    # green, and PR #1236 escaped only by labelling qa/override-reviewer,
    # which discards every finding at once. sdlc-process already requires each
    # flag to be fixed, dispositioned, or routed as follow-up, but nothing in
    # the pipeline reads a disposition -- so gating on flags made the written
    # policy unreachable and left another code change as the only way to turn
    # the status green.

    @patch("reviewer.on_pr_review._review_diff")
    @patch("reviewer.reviewer_runner.invoke_cc_subagent")
    @patch("reviewer.on_pr_review.fetch_existing_review_comments")
    @patch("reviewer.on_pr_review.fetch_existing_issue_comments")
    @patch("reviewer.on_pr_review.publish_review")
    @patch("reviewer.on_pr_review.set_pending")
    @patch("reviewer.on_pr_review.set_success")
    @patch("reviewer.on_pr_review.set_failure")
    @patch("reviewer.on_pr_review.emit_product_event")
    def test_flag_only_is_success_and_still_publishes(
        self, mock_emit, mock_fail, mock_success, mock_pending,
        mock_publish, mock_fetch_issue, mock_fetch, mock_cc,
        mock_diff,
    ):
        """0 block + ≥1 flag → SUCCESS; the flag still reaches the PR.

        A flag is defined to the model as "likely wrong or risky but the
        reviewer is not certain". The author dispositions it under
        sdlc-process; it does not hold the branch.
        """
        mock_diff.return_value = "--- a/x.py\n+++ b/x.py\n+pass\n"
        mock_cc.return_value = json.dumps([
            {
                "severity": "flag",
                "file": "x.py",
                "line": 1,
                "category": "correctness/logic-error",
                "description": "Missing input validation",
                "cited_section": None,
            }
        ])
        mock_fetch.return_value = []
        mock_fetch_issue.return_value = []
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 500,
                "head": {"sha": "flagtest1"},
                "base": {"ref": "dev"},
                "labels": [],
            },
            "repository": {"full_name": "owner/repo"},
        }
        dispatch_pr_review(payload)

        mock_success.assert_called_once()
        assert "1 flag" in mock_success.call_args[0][2]
        mock_fail.assert_not_called()
        # Not gating is not the same as not reporting.
        mock_publish.assert_called_once()

    @patch("reviewer.on_pr_review._review_diff")
    @patch("reviewer.reviewer_runner.invoke_cc_subagent")
    @patch("reviewer.on_pr_review.fetch_existing_review_comments")
    @patch("reviewer.on_pr_review.fetch_existing_issue_comments")
    @patch("reviewer.on_pr_review.publish_review")
    @patch("reviewer.on_pr_review.set_pending")
    @patch("reviewer.on_pr_review.set_success")
    @patch("reviewer.on_pr_review.set_failure")
    @patch("reviewer.on_pr_review.emit_product_event")
    def test_nit_only_is_success(
        self, mock_emit, mock_fail, mock_success, mock_pending,
        mock_publish, mock_fetch_issue, mock_fetch, mock_cc,
        mock_diff,
    ):
        """0 block + 0 flag + ≥1 nit → SUCCESS; nits are advisory, never gate.

        Regression guard: green under both the old and the new gate policy.
        """
        mock_diff.return_value = "--- a/x.py\n+++ b/x.py\n+pass\n"
        mock_cc.return_value = json.dumps([
            {
                "severity": "nit",
                "file": "x.py",
                "line": 1,
                "category": "style/readability",
                "description": "Variable name could be clearer",
                "cited_section": None,
            }
        ])
        mock_fetch.return_value = []
        mock_fetch_issue.return_value = []
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 501,
                "head": {"sha": "nitonly1"},
                "base": {"ref": "dev"},
                "labels": [],
            },
            "repository": {"full_name": "owner/repo"},
        }
        dispatch_pr_review(payload)

        mock_success.assert_called_once()
        assert "1 nit" in mock_success.call_args[0][2]
        mock_fail.assert_not_called()

    # -- Gate policy: errored reviewer = RED, emit reviewer.errored (CHANGE 2)

    @patch("infra.transient_read.time.sleep")
    @patch("reviewer.on_pr_review._review_diff")
    @patch("reviewer.reviewer_runner.invoke_cc_subagent")
    @patch("reviewer.on_pr_review.set_pending")
    @patch("reviewer.on_pr_review.set_success")
    @patch("reviewer.on_pr_review.set_failure")
    @patch("reviewer.on_pr_review.set_error")
    @patch("reviewer.on_pr_review.emit_product_event")
    def test_reviewer_exception_emits_errored_event(
        self, mock_emit, mock_error, mock_fail, mock_success, mock_pending,
        mock_cc, mock_diff, mock_comment_read_sleep,
    ):
        """max_turns / exception / timeout → RED state + reviewer.errored event.

        RED before CHANGE 2: current outer handler emits reviewer.failed, not
        reviewer.errored — the errored-event assertion fails.
        GREEN after CHANGE 2: outer handler emits reviewer.errored.

        Regression guard: set_success must never be called on an errored review.
        """
        mock_diff.return_value = "--- a/x.py\n+++ b/x.py\n+pass\n"
        mock_cc.side_effect = RuntimeError("hit max_turns limit")
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 502,
                "head": {"sha": "errored1"},
                "base": {"ref": "dev"},
                "labels": [],
            },
            "repository": {"full_name": "owner/repo"},
        }
        with contextlib.suppress(Exception):
            dispatch_pr_review(payload)

        mock_success.assert_not_called()
        mock_error.assert_called_once()  # exceptions use set_error (ERROR state), not set_failure
        errored_events = [
            c for c in mock_emit.call_args_list
            if c[0][:2] == ("reviewer", "errored")
        ]
        assert len(errored_events) == 1  # RED: currently emits "reviewer.failed"
        failed_events = [
            c for c in mock_emit.call_args_list
            if c[0][:2] == ("reviewer", "failed")
        ]
        assert len(failed_events) == 1  # backward-compat emit also present

    def test_execution_error_propagates_after_red_check(self, monkeypatch):
        """An execution failure (e.g. the ``claude`` binary missing) sets the
        RED check AND propagates out of on_pr_review, so the reviewer gate keeps
        the SHA retryable instead of marking it reviewed. Swallowing it is what
        let a failed review silently get recorded as done."""
        from reviewer import on_pr_review as opr

        monkeypatch.setenv("CORSIA_REVIEWER_MODEL", "claude-opus-4-8")
        red = {}
        monkeypatch.setattr(opr, "emit_product_event", lambda *a, **k: None)
        monkeypatch.setattr(
            opr, "set_error", lambda *a, **k: red.__setitem__("set", True),
        )

        def _boom(*a, **k):
            raise FileNotFoundError("[Errno 2] No such file or directory: 'claude'")

        monkeypatch.setattr(opr, "_run_cc", _boom)
        monkeypatch.setattr(opr, "fetch_existing_review_comments", lambda *_: [])
        monkeypatch.setattr(opr, "fetch_existing_issue_comments", lambda *_: [])

        payload = {
            "action": "opened",
            "pull_request": {
                "number": 519, "head": {"sha": "binmissing"},
                "base": {"ref": "dev"}, "labels": [],
            },
            "repository": {"full_name": "owner/repo"},
        }
        with pytest.raises(FileNotFoundError):
            opr.on_pr_review("webhook.github.pr_opened", payload)
        assert red.get("set"), "red check must be set before propagation"


class TestUnusableReviewerAccountPublishesAFailedCheck:
    """A limited or unpermitted reviewer account is model-unavailable: red, no verdict, no pending strand."""

    @pytest.fixture(autouse=True)
    def _setup(self, _isolated_db):
        yield

    @staticmethod
    def _limit_default_account():
        from connections import harness_usage
        from connections.account_receipts import capture_receipt
        from connections.vendor_accounts import default_account
        from infra.database import get_db

        db = get_db()
        account = default_account("claude-code", db=db)
        harness_usage.record_limited(
            provider="claude-code",
            receipt=capture_receipt("claude-code", account_id=account["id"], db=db),
            reason="usage_limit", resets_at="2099-01-01T00:00:00+00:00",
            source="rate_limit_update", db=db,
        )
        db.commit()

    @patch("reviewer.on_pr_review._review_diff")
    @patch("reviewer.reviewer_runner.invoke_cc_subagent")
    @patch("reviewer.on_pr_review.fetch_existing_review_comments", return_value=[])
    @patch("reviewer.on_pr_review.fetch_existing_issue_comments", return_value=[])
    @patch("reviewer.on_pr_review.publish_review")
    @patch("reviewer.on_pr_review.emit_product_event")
    @patch("reviewer.reviewer_runner.emit_product_event")
    def test_limited_account_ends_failed_with_no_verdict_and_no_pending(
        self, _runner_emit, _emit, mock_publish, _issues, _reviews, mock_cc, mock_diff,
    ):
        mock_diff.return_value = "--- a/x.py\n+++ b/x.py\n+pass\n"
        self._limit_default_account()
        states = []
        with contextlib.ExitStack() as stack:
            for module, names in (
                ("reviewer.reviewer_runner", ("set_pending", "set_failure")),
                ("reviewer.on_pr_review",
                 ("set_pending", "set_success", "set_failure", "set_error")),
            ):
                for name in names:
                    stack.enter_context(patch(
                        f"{module}.{name}",
                        side_effect=lambda *a, _n=name, **k: states.append((_n, a[-1])),
                    ))
            with contextlib.suppress(Exception):
                dispatch_pr_review({
                    "action": "opened",
                    "pull_request": {"number": 601, "head": {"sha": "limited1"},
                                     "base": {"ref": "dev"}, "labels": []},
                    "repository": {"full_name": "owner/repo"},
                })

        mock_cc.assert_not_called()
        mock_publish.assert_not_called()
        # The last check write is the failure: nothing left pending, nothing passed.
        assert states[-1][0] == "set_failure"
        assert "model-unavailable" in states[-1][1] and "usage_limited" in states[-1][1]
        assert not [name for name, _ in states if name in ("set_success", "set_error")]
