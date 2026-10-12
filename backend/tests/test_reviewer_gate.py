
from __future__ import annotations

import json
import logging
from unittest.mock import ANY, DEFAULT, MagicMock, patch

import pytest
from reviewer.gh_helpers import STATUS_WRITE_STAMP
from reviewer_gate_fixtures import (  # noqa: F401
    _DEFAULT_PR_INFO,
    _call_gate,
    _disable_analytics,
    _reload_gate,
)


class TestReviewerVerdictReturn:
    def test_override_returns_no_clean_verdict(self):
        from reviewer import on_pr_review

        payload = {
            "repository": {"full_name": "owner/repo"},
            "pull_request": {
                "number": 42,
                "head": {"sha": "abc123def"},
                "labels": [{"name": "qa/override-reviewer"}],
            },
        }

        with patch.object(on_pr_review, "set_success") as set_success:
            verdict = on_pr_review.on_pr_review("pull_request", payload)

        assert verdict == {
            "verdict": "skipped",
            "summary": "qa/override-reviewer -- review skipped by override",
            "model_label": "qa/override-reviewer",
        }
        set_success.assert_called_once_with(
            "owner/repo",
            "abc123def",
            "qa/override-reviewer -- review skipped by override",
        )

    def test_process_findings_returns_clean_verdict(self):
        from reviewer import on_pr_review

        with patch.object(on_pr_review, "parse_findings", return_value=[]), \
             patch.object(on_pr_review, "fetch_existing_review_comments", return_value=[]), \
             patch.object(on_pr_review, "fetch_existing_issue_comments", return_value=[]), \
             patch.object(on_pr_review, "classify_review_findings", return_value=([], [], 0)), \
             patch.object(on_pr_review, "publish_review"), \
             patch.object(on_pr_review, "set_success"):
            verdict = on_pr_review._process_findings(
                "[]", 42, "owner/repo", "abc123def", "codex",
            )

        assert verdict["verdict"] == "clean"
        assert verdict["block_count"] == 0
        assert verdict["flag_count"] == 0
        assert verdict["model_label"] == "codex"

    def test_process_findings_returns_code_fail_verdict(self):
        from reviewer import on_pr_review

        findings = [{
            "severity": "block",
            "category": "correctness/logic-error",
            "file": "backend/x.py",
        }]

        with patch.object(on_pr_review, "parse_findings", return_value=findings), \
             patch.object(on_pr_review, "fetch_existing_review_comments", return_value=[]), \
             patch.object(on_pr_review, "fetch_existing_issue_comments", return_value=[]), \
             patch.object(on_pr_review, "classify_review_findings", return_value=(findings, [], 0)), \
             patch.object(on_pr_review, "publish_review"), \
             patch.object(on_pr_review, "set_failure"):
            verdict = on_pr_review._process_findings(
                "[]", 42, "owner/repo", "abc123def", "codex",
            )

        assert verdict["verdict"] == "code_fail"
        assert verdict["block_count"] == 1
        assert verdict["flag_count"] == 0
        assert verdict["finding_count"] == 1

    def test_reraised_resolved_finding_is_published_without_changing_the_verdict(self):
        from reviewer import on_pr_review
        from reviewer.reviewer_threads import format_threaded_comment_body

        finding = {
            "severity": "block",
            "category": "correctness/logic-error",
            "file": "backend/x.py",
            "line": 1,
            "description": "Still broken.",
            "cited_section": None,
        }
        parent = {
            "id": 1,
            "body": format_threaded_comment_body(finding),
            "pull_request_url": "https://api.github.com/repos/owner/repo/pulls/42",
        }
        resolved_reply = {
            "id": 2,
            "body": "<!-- corsia-review-resolved -->",
            "in_reply_to_id": 1,
        }

        with patch.object(on_pr_review, "parse_findings", return_value=[finding]), \
             patch.object(
                 on_pr_review, "fetch_existing_review_comments",
                 return_value=[parent, resolved_reply],
             ), \
             patch.object(on_pr_review, "fetch_existing_issue_comments", return_value=[]), \
             patch("reviewer.review_thread_lifecycle.fetch_existing_review_threads", return_value=None), \
             patch.object(on_pr_review, "publish_review") as publish_review, \
             patch.object(on_pr_review, "set_failure"):
            verdict = on_pr_review._process_findings(
                "[]", 42, "owner/repo", "abc123def", "codex",
            )

        assert verdict["verdict"] == "code_fail"
        assert verdict["block_count"] == 1
        assert publish_review.call_args.kwargs["new_threads"] == [finding]

    def test_approach_churn_is_visible_in_the_owner_verdict_summary(self):
        from reviewer import on_pr_review

        finding = {
            "severity": "block",
            "file": "",
            "line": 0,
            "category": "review-process/approach-churn",
            "description": "The fixes are spawning parallel owners.",
            "cited_section": None,
        }
        with patch.object(on_pr_review, "parse_findings", return_value=[finding]), \
             patch.object(on_pr_review, "fetch_existing_review_comments", return_value=[]), \
             patch.object(on_pr_review, "fetch_existing_issue_comments", return_value=[]), \
             patch.object(on_pr_review, "classify_review_findings", return_value=([finding], [], 0)), \
             patch.object(on_pr_review, "publish_review"), \
             patch.object(on_pr_review, "set_failure"):
            verdict = on_pr_review._process_findings(
                "ignored", 42, "owner/repo", "abc123", "codex",
            )

        assert "approach reset required" in verdict["summary"]

    def test_approach_churn_is_a_required_action_and_one_grouped_finding(self):
        """Reset mode must stop the patch queue, not merely rename a code fail."""
        from reviewer import on_pr_review

        approach = {
            "severity": "block",
            "file": "",
            "line": 0,
            "category": "review-process/approach-churn",
            "description": "The local fixes share one broken naming approach.",
            "cited_section": None,
        }
        local = {
            "severity": "block",
            "file": "backend/example.py",
            "line": 17,
            "category": "standards/eng-arch",
            "description": "This provider session was renamed as a thread.",
            "cited_section": "EAS5",
        }
        published = []

        with patch.object(
            on_pr_review, "parse_findings", return_value=[approach, local],
        ), patch.object(
            on_pr_review, "fetch_existing_review_comments", return_value=[],
        ), patch.object(
            on_pr_review, "fetch_existing_issue_comments", return_value=[],
        ), patch.object(
            on_pr_review,
            "publish_review",
            side_effect=lambda **kwargs: published.append(kwargs),
        ), patch.object(
            on_pr_review, "set_failure", return_value=True,
        ) as set_failure:
            verdict = on_pr_review._process_findings(
                "ignored", 42, "owner/repo", "abc123", "codex",
            )

        assert verdict["verdict"] == "code_fail"
        assert verdict["required_action"] == "approach_reset"
        assert set_failure.call_args.args[2].startswith(
            "STOP: approach reset required"
        )
        grouped = published[0]["findings"]
        assert len(grouped) == 1
        assert grouped[0]["category"] == "review-process/approach-churn"
        assert "This provider session was renamed as a thread." in (
            grouped[0]["description"]
        )

    def test_approach_grouping_does_not_resolve_still_present_local_evidence(self):
        """Suppressing the patch queue must not falsify finding history."""
        from reviewer import on_pr_review
        from reviewer.reviewer_threads import format_threaded_comment_body

        approach = {
            "severity": "block", "file": "", "line": 0,
            "category": "review-process/approach-churn",
            "description": "These defects share one transformation cause.",
            "cited_section": None,
        }
        local = {
            "severity": "block", "file": "backend/example.py", "line": 17,
            "category": "standards/eng-arch",
            "description": "A provider session is mislabeled as a thread.",
            "cited_section": "EAS5",
        }
        prior_local = {
            "id": 7,
            "body": format_threaded_comment_body(local),
            "pull_request_url": "https://api.github.test/repos/owner/repo/pulls/42",
        }
        published = []

        with patch.object(
            on_pr_review, "parse_findings", return_value=[approach, local],
        ), patch.object(
            on_pr_review,
            "fetch_existing_review_comments",
            return_value=[prior_local],
        ), patch.object(
            on_pr_review, "fetch_existing_issue_comments", return_value=[],
        ), patch.object(
            on_pr_review,
            "publish_review",
            side_effect=lambda **kwargs: published.append(kwargs),
        ), patch.object(on_pr_review, "set_failure", return_value=True):
            on_pr_review._process_findings(
                "ignored", 42, "owner/repo", "abc123", "codex",
            )

        assert published[0]["resolve_replies"] == []

    def test_flags_and_nits_publish_but_do_not_fail_the_gate(self):
        """Only a block holds the gate.

        sdlc-process requires every flag to be fixed, dispositioned, or routed
        as follow-up. Failing the check on flags made disposition unreachable:
        PR #1190 ran 15+ rounds with zero blocks and never went green, because
        the only act that changed the status was another code change.
        """
        from reviewer import on_pr_review

        findings = [
            {
                "severity": "flag",
                "category": "correctness/logic-error",
                "file": "backend/x.py",
            },
            {
                "severity": "nit",
                "category": "style/readability",
                "file": "backend/x.py",
            },
        ]
        published = []

        with patch.object(on_pr_review, "parse_findings", return_value=findings), \
             patch.object(on_pr_review, "fetch_existing_review_comments", return_value=[]), \
             patch.object(on_pr_review, "fetch_existing_issue_comments", return_value=[]), \
             patch.object(on_pr_review, "classify_review_findings", return_value=(findings, [], 0)), \
             patch.object(on_pr_review, "publish_review", side_effect=lambda **kw: published.append(kw)), \
             patch.object(on_pr_review, "set_failure") as failure, \
             patch.object(on_pr_review, "set_success", return_value=True) as success:
            verdict = on_pr_review._process_findings(
                "[]", 42, "owner/repo", "abc123def", "codex",
            )

        assert verdict["verdict"] == "clean"
        assert success.called
        assert not failure.called
        # The findings still reach the PR; they simply do not gate it.
        assert len(published) == 1
        assert published[0]["findings"] == findings
        assert "1 flag, 1 nit" in success.call_args[0][2]


class TestReviewerPublicationFailures:
    """A partially published review is not a terminal review."""

    @pytest.mark.parametrize(
        "failure",
        [
            "lost-finding",
            "lost-block",
            "review-comment-history",
            "issue-comment-history",
            "clean-status",
            "code-fail-status",
        ],
    )
    def test_failed_publication_stays_loud_and_retryable(
        self, _reload_gate, failure,
    ):
        from events.connectors import github_reviewer_notifications
        from reviewer import on_pr_review as reviewer_mod
        from reviewer.review_publication import ReviewPublicationError

        findings = []
        if failure == "code-fail-status":
            # Must be a block: only a block produces a code_fail verdict, and
            # this case exercises a failed publication of that status.
            findings = [{
                "severity": "block",
                "category": "correctness/logic-error",
                "file": "backend/x.py",
            }]

        def run_cc(*_args, **_kwargs):
            return reviewer_mod._process_findings(
                "[]", 42, "owner/repo", "abc123def", "codex",
            )

        payload = {
            "repository": {"full_name": "owner/repo"},
            "pull_request": {
                "number": 42,
                "head": {"sha": "abc123def"},
                "base": {"ref": "dev"},
                "labels": [],
            },
        }
        with patch.multiple(
            reviewer_mod,
            _run_cc=DEFAULT,
            parse_findings=DEFAULT,
            fetch_existing_review_comments=DEFAULT,
            fetch_existing_issue_comments=DEFAULT,
            classify_review_findings=DEFAULT,
            publish_review=DEFAULT,
            set_success=DEFAULT,
            set_failure=DEFAULT,
            set_error=DEFAULT,
            emit_product_event=DEFAULT,
        ) as reviewer_mocks, patch.multiple(
            _reload_gate,
            emit_product_event=DEFAULT,
        ), patch.multiple(
            github_reviewer_notifications,
            notify_reviewer_error=DEFAULT,
            notify_reviewer_verdict=DEFAULT,
        ) as notification_mocks:
            reviewer_mocks["_run_cc"].side_effect = run_cc
            reviewer_mocks["parse_findings"].return_value = findings
            reviewer_mocks["fetch_existing_review_comments"].return_value = (
                None if failure == "review-comment-history" else []
            )
            reviewer_mocks["fetch_existing_issue_comments"].return_value = (
                None if failure == "issue-comment-history" else []
            )
            reviewer_mocks["classify_review_findings"].return_value = (findings, [], 0)
            if failure == "lost-finding":
                reviewer_mocks["publish_review"].side_effect = (
                    ReviewPublicationError(
                        "review publication failed: NIT "
                        "[style/readability] backend/x.py:3 NOT PUBLISHED",
                        lost_block=False,
                    )
                )
            elif failure == "lost-block":
                reviewer_mocks["publish_review"].side_effect = (
                    ReviewPublicationError(
                        "review LOST 1 BLOCK finding(s): BLOCK "
                        "[correctness/race-condition] backend/x.py:12 "
                        "NOT PUBLISHED",
                        lost_block=True,
                    )
                )
            reviewer_mocks["set_success"].return_value = failure != "clean-status"
            reviewer_mocks["set_failure"].return_value = (
                failure != "code-fail-status"
            )
            reviewer_mocks["set_error"].return_value = True
            notification_mocks["notify_reviewer_verdict"].return_value = "no_owner"

            _reload_gate._run_reviewer(
                "owner/repo", 42, "abc123def", "dev",
                "webhook.github.check_suite_completed", payload,
            )

        if failure.endswith("comment-history"):
            reviewer_mocks["classify_review_findings"].assert_not_called()
        if failure in {"clean-status", "code-fail-status"}:
            reviewer_mocks[
                "set_success" if failure == "clean-status" else "set_failure"
            ].assert_called_once()
        else:
            reviewer_mocks["set_success"].assert_not_called()
            reviewer_mocks["set_failure"].assert_not_called()
        reviewer_mocks["set_error"].assert_called_once()
        if failure == "lost-block":
            # The one shortfall that must survive the 140-char status budget.
            status_text = reviewer_mocks["set_error"].call_args[0][2]
            assert "LOST 1 BLOCK" in status_text
            assert "[correctness/race-condition]" in status_text
        notification_mocks["notify_reviewer_error"].assert_called_once()
        assert _reload_gate.reviewer_gate_state.get_reviewed_sha("owner/repo#42") is None

    def test_failed_override_status_publication_stays_retryable(self, _reload_gate):
        from events.connectors import github_reviewer_notifications
        from reviewer import on_pr_review as reviewer_mod

        payload = {
            "repository": {"full_name": "owner/repo"},
            "pull_request": {
                "number": 42,
                "head": {"sha": "abc123def"},
                "base": {"ref": "dev"},
                "labels": [{"name": "qa/override-reviewer"}],
            },
        }
        with patch.object(reviewer_mod, "set_success", return_value=False), \
             patch.object(reviewer_mod, "emit_product_event"), \
             patch.object(_reload_gate, "emit_product_event"), \
             patch.object(
                 github_reviewer_notifications, "notify_reviewer_error",
             ) as notify_error:
            _reload_gate._run_reviewer(
                "owner/repo", 42, "abc123def", "dev",
                "webhook.github.check_suite_completed", payload,
            )

        notify_error.assert_called_once()
        assert _reload_gate.reviewer_gate_state.get_reviewed_sha("owner/repo#42") is None


class TestGateFires:
    def test_gate_fires_on_green_suite(self, _reload_gate):
        gate_mod = _reload_gate

        with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=_DEFAULT_PR_INFO), \
             patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch.object(gate_mod, "_run_reviewer") as mock_run, \
             patch.object(gate_mod, "emit_product_event"):

            _call_gate(gate_mod)

        mock_run.assert_called_once()
        args = mock_run.call_args[1] if mock_run.call_args[1] else mock_run.call_args[0]
        assert mock_run.called

    def test_gate_passes_correct_args_to_reviewer(self, _reload_gate):
        gate_mod = _reload_gate
        captured = {}

        def capture_run(
            repo, pr_number, head_sha, base_ref, internal_event, pr_payload, loop,
        ):
            captured.update(
                repo=repo, pr_number=pr_number, head_sha=head_sha,
                base_ref=base_ref, internal_event=internal_event,
                pr_payload=pr_payload,
            )

        pr_info = {**_DEFAULT_PR_INFO, "headRefOid": "deadbeef"}
        with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=pr_info), \
             patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch.object(gate_mod, "_run_reviewer", side_effect=capture_run), \
             patch.object(gate_mod, "emit_product_event"):

            _call_gate(gate_mod, repo="org/proj", pr_number=99, head_sha="deadbeef",
                       internal_event="webhook.github.check_suite_completed")

        assert captured["repo"] == "org/proj"
        assert captured["pr_number"] == 99
        assert captured["head_sha"] == "deadbeef"
        assert captured["base_ref"] == "dev"
        assert captured["pr_payload"]["pull_request"]["base"] == {
            "ref": "dev", "sha": "base123def",
        }
        assert captured["pr_payload"]["pull_request"]["title"] == (
            "Contain reviewer scope"
        )
        assert captured["pr_payload"]["pull_request"]["body"] == (
            "## Scope\nOnly review the changed release slice."
        )



class TestGateSkipsNonSuccess:
    @pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "neutral"])
    def test_gate_skips_non_success_suite(self, _reload_gate, conclusion):
        gate_mod = _reload_gate

        with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=_DEFAULT_PR_INFO), \
             patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch.object(gate_mod, "_run_reviewer") as mock_run, \
             patch.object(gate_mod, "emit_product_event"):

            _call_gate(gate_mod, conclusion=conclusion)

        mock_run.assert_not_called()

    def test_gate_skips_empty_conclusion(self, _reload_gate):
        gate_mod = _reload_gate

        with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=_DEFAULT_PR_INFO), \
             patch.object(gate_mod, "_run_reviewer") as mock_run, \
             patch.object(gate_mod, "emit_product_event"):

            _call_gate(gate_mod, conclusion="")

        mock_run.assert_not_called()



class TestSkipGuard:
    def test_skip_guard_prevents_double_fire(self, _reload_gate):
        gate_mod = _reload_gate

        gate_mod.reviewer_gate_state.mark_reviewed("owner/repo#42", "abc123def")

        with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=_DEFAULT_PR_INFO), \
             patch.object(gate_mod, "_run_reviewer") as mock_run, \
             patch.object(gate_mod, "emit_product_event"):

            _call_gate(gate_mod)  # same sha

        mock_run.assert_not_called()

    def test_skip_guard_emits_event(self, _reload_gate):
        gate_mod = _reload_gate
        emitted = []

        gate_mod.reviewer_gate_state.mark_reviewed("owner/repo#42", "abc123def")

        with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=_DEFAULT_PR_INFO), \
             patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch.object(gate_mod, "_run_reviewer"), \
             patch.object(gate_mod, "emit_product_event",
                          side_effect=lambda s, e, **kw: emitted.append((s, e))):

            _call_gate(gate_mod)

        assert ("reviewer_gate", "skip_guard") in emitted

    def test_different_sha_bypasses_skip_guard(self, _reload_gate):
        gate_mod = _reload_gate

        gate_mod.reviewer_gate_state.mark_reviewed("owner/repo#42", "old_sha")
        pr_info = {**_DEFAULT_PR_INFO, "headRefOid": "new_sha"}

        with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=pr_info), \
             patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch.object(gate_mod, "_run_reviewer") as mock_run, \
             patch.object(gate_mod, "emit_product_event"):

            _call_gate(gate_mod, head_sha="new_sha")

        mock_run.assert_called_once()



class TestSkipGuardWrite:
    def test_gate_wakes_registered_pr_owner_on_clean_reviewer_verdict(
        self, _reload_gate
    ):
        """A clean reviewer verdict wakes the registered PR owner with PR/head context."""
        from unittest.mock import AsyncMock

        from tests.event_routing_helpers import mk_session, register_pr

        gate_mod = _reload_gate
        delivered = AsyncMock(return_value=True)
        mk_session("owner-thread")
        register_pr("owner/repo", 42, "owner-thread")

        verdict = {
            "verdict": "clean",
            "summary": "0 block, 0 flag, 0 nit -- reviewed by codex",
            "block_count": 0,
            "flag_count": 0,
            "nit_count": 0,
            "finding_count": 0,
            "model_label": "codex",
        }

        with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=_DEFAULT_PR_INFO), \
             patch.object(gate_mod, "emit_product_event"), \
             patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch("reviewer.on_pr_review.on_pr_review", return_value=verdict), \
             patch("events.delivery.deliver_to_thread", delivered):
            _call_gate(gate_mod)

        delivered.assert_awaited_once()
        args, _ = delivered.call_args
        assert args[0] == "owner-thread"
        assert args[1] == "reviewer.verdict"
        assert args[2] == "PR #42 reviewer verdict: clean"
        assert args[3]["repo"] == "owner/repo"
        assert args[3]["pr_number"] == 42
        assert args[3]["head_sha"] == "abc123def"
        assert args[3]["verdict"] == "clean"

    def test_gate_wakes_registered_pr_owner_on_code_fail_reviewer_verdict(
        self, _reload_gate
    ):
        """A code-fail reviewer verdict wakes the registered PR owner with counts."""
        from unittest.mock import AsyncMock

        from tests.event_routing_helpers import mk_session, register_pr

        gate_mod = _reload_gate
        delivered = AsyncMock(return_value=True)
        mk_session("owner-thread")
        register_pr("owner/repo", 42, "owner-thread")

        verdict = {
            "verdict": "code_fail",
            "required_action": "approach_reset",
            "summary": "1 block, 2 flag, 0 nit -- reviewed by codex",
            "block_count": 1,
            "flag_count": 2,
            "nit_count": 0,
            "finding_count": 3,
            "model_label": "codex",
        }

        with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=_DEFAULT_PR_INFO), \
             patch.object(gate_mod, "emit_product_event"), \
             patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch("reviewer.on_pr_review.on_pr_review", return_value=verdict), \
             patch("events.delivery.deliver_to_thread", delivered):
            _call_gate(gate_mod)

        delivered.assert_awaited_once()
        args, _ = delivered.call_args
        assert args[0] == "owner-thread"
        assert args[1] == "reviewer.verdict"
        assert args[2] == "PR #42 reviewer verdict: code_fail"
        assert args[3]["repo"] == "owner/repo"
        assert args[3]["pr_number"] == 42
        assert args[3]["head_sha"] == "abc123def"
        assert args[3]["verdict"] == "code_fail"
        assert args[3]["required_action"] == "approach_reset"
        assert args[3]["block_count"] == 1
        assert args[3]["flag_count"] == 2

    def test_gate_wakes_pr_owner_with_flag_counts_on_a_clean_verdict(
        self, _reload_gate
    ):
        """Not gating on flags must not make flags invisible.

        Flags no longer hold the check, so the owner wake is the surface that
        carries them. If this regresses, a flag becomes a finding nobody is
        told about, which is worse than the runaway it replaced.
        """
        from unittest.mock import AsyncMock

        from tests.event_routing_helpers import mk_session, register_pr

        gate_mod = _reload_gate
        delivered = AsyncMock(return_value=True)
        mk_session("owner-thread")
        register_pr("owner/repo", 42, "owner-thread")

        verdict = {
            "verdict": "clean",
            "summary": "0 block, 3 flag, 1 nit -- reviewed by codex",
            "block_count": 0,
            "flag_count": 3,
            "nit_count": 1,
            "finding_count": 4,
            "model_label": "codex",
        }

        with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=_DEFAULT_PR_INFO), \
             patch.object(gate_mod, "emit_product_event"), \
             patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch("reviewer.on_pr_review.on_pr_review", return_value=verdict), \
             patch("events.delivery.deliver_to_thread", delivered):
            _call_gate(gate_mod)

        delivered.assert_awaited_once()
        args, _ = delivered.call_args
        assert args[3]["verdict"] == "clean"
        assert args[3]["block_count"] == 0
        assert args[3]["flag_count"] == 3
        assert args[3]["nit_count"] == 1
        assert "3 flag" in args[3]["summary"]

    def test_gate_does_not_mark_reviewed_when_verdict_wake_fails(
        self, _reload_gate
    ):
        """A verdict wake failure must not look completed in gate state."""
        from unittest.mock import AsyncMock

        from tests.event_routing_helpers import mk_session, register_pr

        gate_mod = _reload_gate
        delivered = AsyncMock(return_value=False)
        mk_session("owner-thread")
        register_pr("owner/repo", 42, "owner-thread")

        verdict = {
            "verdict": "clean",
            "summary": "0 block, 0 flag, 0 nit -- reviewed by codex",
            "block_count": 0,
            "flag_count": 0,
            "nit_count": 0,
            "finding_count": 0,
            "model_label": "codex",
        }

        with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=_DEFAULT_PR_INFO), \
             patch.object(gate_mod, "emit_product_event"), \
             patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch("reviewer.on_pr_review.on_pr_review", return_value=verdict), \
             patch("events.delivery.deliver_to_thread", delivered):
            _call_gate(gate_mod)

        delivered.assert_awaited_once()
        assert gate_mod.reviewer_gate_state.get_reviewed_sha("owner/repo#42") is None

    def test_gate_marks_override_skipped_without_verdict_wake(self, _reload_gate):
        """Override skips review without emitting a clean reviewer.verdict wake."""
        from unittest.mock import AsyncMock

        gate_mod = _reload_gate
        pr_info = {
            **_DEFAULT_PR_INFO,
            "labels": [{"name": "qa/override-reviewer"}],
        }
        delivered = AsyncMock(return_value=True)

        with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=pr_info), \
             patch.object(gate_mod, "emit_product_event"), \
             patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch("events.delivery.deliver_to_thread", delivered), \
             patch("reviewer.on_pr_review.set_success"):
            _call_gate(gate_mod)

        delivered.assert_not_awaited()
        assert gate_mod.reviewer_gate_state.get_reviewed_sha("owner/repo#42") == "abc123def"

    def test_gate_marks_reviewed_when_verdict_has_no_registered_owner(
        self, _reload_gate
    ):
        """Missing PR owner is loud but must not turn a valid review red."""
        from events.connectors import github_reviewer_notifications

        gate_mod = _reload_gate
        verdict = {
            "verdict": "clean",
            "summary": "0 block, 0 flag, 0 nit -- reviewed by codex",
            "block_count": 0,
            "flag_count": 0,
            "nit_count": 0,
            "finding_count": 0,
            "model_label": "codex",
        }

        with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=_DEFAULT_PR_INFO), \
             patch.object(gate_mod, "emit_product_event"), \
             patch.object(
                 github_reviewer_notifications, "emit_product_event",
             ) as emit_event, \
             patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch("reviewer.on_pr_review.on_pr_review", return_value=verdict), \
             patch("events.delivery.owning_thread_for_identity", return_value=None), \
             patch("reviewer.reviewer_status.set_error") as set_error:
            _call_gate(gate_mod)

        set_error.assert_not_called()
        assert any(
            call.args[:2] == ("reviewer_gate", "reviewer_verdict_owner_missing")
            and call.kwargs["properties"] == {
                "repo": "owner/repo",
                "pr_number": 42,
                "head_sha": "abc123de",
                "verdict": "clean",
            }
            for call in emit_event.call_args_list
        )
        assert gate_mod.reviewer_gate_state.get_reviewed_sha("owner/repo#42") == "abc123def"

    @pytest.mark.parametrize(
        ("invalid_verdict", "reason", "status_text"),
        [
            (
                None,
                "reviewer returned no terminal verdict",
                "qa/llm-reviewer: reviewer returned no terminal verdict; retry required",
            ),
            (
                "bad-result",
                "reviewer returned invalid terminal verdict (str)",
                "qa/llm-reviewer: reviewer returned invalid terminal verdict (str); retry required",
            ),
        ],
    )
    def test_invalid_verdict_reports_error_and_allows_current_head_refire(
        self, _reload_gate, invalid_verdict, reason, status_text,
    ):
        """Invalid reviewer output is loud, retryable, and does not set the skip guard."""
        from events.connectors import github_reviewer_notifications

        gate_mod = _reload_gate
        clean_verdict = {"verdict": "clean"}

        with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=_DEFAULT_PR_INFO), \
             patch.object(github_reviewer_notifications, "emit_product_event") as emit_event, \
             patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch("reviewer.on_pr_review.on_pr_review", side_effect=[invalid_verdict, clean_verdict]), \
             patch.object(github_reviewer_notifications, "notify_reviewer_terminal_error") as notify_error, \
             patch.object(github_reviewer_notifications, "notify_reviewer_verdict", return_value="no_owner"), \
             patch("reviewer.reviewer_status.has_existing_failure_or_error", return_value=False), \
             patch("reviewer.reviewer_status.set_error") as set_error:
            _call_gate(gate_mod)
            assert gate_mod.reviewer_gate_state.get_reviewed_sha("owner/repo#42") is None
            _call_gate(gate_mod)

        set_error.assert_called_once_with("owner/repo", "abc123def", status_text)
        notify_error.assert_called_once_with("owner/repo", 42, "abc123def", reason, ANY)
        expected_event = "reviewer_no_terminal_result" if invalid_verdict is None else "reviewer_unexpected_result"
        expected_properties = {
            "repo": "owner/repo",
            "pr_number": 42,
            "head_sha": "abc123de",
        }
        if invalid_verdict is not None:
            expected_properties["result_type"] = "str"
        assert any(
            call.args[:2] == ("reviewer_gate", expected_event)
            and call.kwargs["properties"] == expected_properties
            for call in emit_event.call_args_list
        )
        assert gate_mod.reviewer_gate_state.get_reviewed_sha("owner/repo#42") == "abc123def"

    def test_none_verdict_preserves_prior_specific_failure(self, _reload_gate):
        """A terminal None must not hide the reviewer's more specific failure."""
        from events.connectors import github_reviewer_notifications

        gate_mod = _reload_gate
        standing_failure = {
            "state": "failure",
            "description": "qa/llm-reviewer: model-unavailable-no-fallback",
        }

        with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=_DEFAULT_PR_INFO), \
             patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch("reviewer.on_pr_review.on_pr_review", return_value=None), \
             patch.object(github_reviewer_notifications, "emit_product_event") as emit_event, \
             patch.object(github_reviewer_notifications, "notify_reviewer_terminal_error") as notify_error, \
             patch("reviewer.reviewer_status.get_commit_status_for_context", return_value=standing_failure), \
             patch("reviewer.reviewer_status.set_error") as set_error:
            _call_gate(gate_mod)

        set_error.assert_not_called()
        notify_error.assert_not_called()
        assert any(
            call.args[:2] == ("reviewer_gate", "reviewer_no_terminal_result")
            and call.kwargs["properties"] == {
                "repo": "owner/repo",
                "pr_number": 42,
                "head_sha": "abc123de",
            }
            for call in emit_event.call_args_list
        )
        assert gate_mod.reviewer_gate_state.get_reviewed_sha("owner/repo#42") is None

    def test_gate_sets_error_status_when_verdict_wake_fails(
        self, _reload_gate
    ):
        """Delivery failure must leave a visible retryable PR status."""
        from unittest.mock import AsyncMock

        from tests.event_routing_helpers import mk_session, register_pr

        gate_mod = _reload_gate
        delivered = AsyncMock(return_value=False)
        mk_session("owner-thread")
        register_pr("owner/repo", 42, "owner-thread")

        verdict = {
            "verdict": "clean",
            "summary": "0 block, 0 flag, 0 nit -- reviewed by codex",
            "block_count": 0,
            "flag_count": 0,
            "nit_count": 0,
            "finding_count": 0,
            "model_label": "codex",
        }

        with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=_DEFAULT_PR_INFO), \
             patch.object(gate_mod, "emit_product_event"), \
             patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch("reviewer.on_pr_review.on_pr_review", return_value=verdict), \
             patch("events.delivery.deliver_to_thread", delivered), \
             patch("reviewer.reviewer_status.set_error") as set_error:
            _call_gate(gate_mod)

        delivered.assert_awaited_once()
        set_error.assert_called_once_with(
            "owner/repo",
            "abc123def",
            "qa/llm-reviewer: owner wake delivery failed; retry required",
        )
        assert gate_mod.reviewer_gate_state.get_reviewed_sha("owner/repo#42") is None

    def test_gate_marks_sha_after_success(self, _reload_gate):
        gate_mod = _reload_gate
        verdict = {
            "verdict": "clean",
            "summary": "0 block, 0 flag, 0 nit -- reviewed by codex",
            "block_count": 0,
            "flag_count": 0,
            "nit_count": 0,
            "finding_count": 0,
            "model_label": "codex",
        }

        with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=_DEFAULT_PR_INFO), \
             patch.object(gate_mod, "emit_product_event"), \
             patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch("reviewer.on_pr_review.on_pr_review", return_value=verdict), \
             patch("events.delivery.owning_thread_for_identity", return_value=None):
            _call_gate(gate_mod)

        assert gate_mod.reviewer_gate_state.get_reviewed_sha("owner/repo#42") == "abc123def"

    def test_gate_marks_sha_prevents_second_call(self, _reload_gate):
        gate_mod = _reload_gate
        fire_count = [0]

        def fake_reviewer(*a, **kw):
            fire_count[0] += 1
            return {
                "verdict": "clean",
                "summary": "0 block, 0 flag, 0 nit -- reviewed by codex",
                "block_count": 0,
                "flag_count": 0,
                "nit_count": 0,
                "finding_count": 0,
                "model_label": "codex",
            }

        with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=_DEFAULT_PR_INFO), \
             patch.object(gate_mod, "emit_product_event"), \
             patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch("events.delivery.owning_thread_for_identity", return_value=None), \
             patch("reviewer.on_pr_review.on_pr_review", side_effect=fake_reviewer):

            _call_gate(gate_mod)
            _call_gate(gate_mod)

        assert fire_count[0] == 1

    def test_gate_no_mark_on_exception(self, _reload_gate):
        gate_mod = _reload_gate

        with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=_DEFAULT_PR_INFO), \
             patch.object(gate_mod, "emit_product_event"), \
             patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch("reviewer.on_pr_review.on_pr_review",
                   side_effect=RuntimeError("reviewer exploded")), \
             patch("events.delivery.owning_thread_for_identity", return_value=None):
            _call_gate(gate_mod)

        assert gate_mod.reviewer_gate_state.get_reviewed_sha("owner/repo#42") is None

    def test_gate_wakes_owner_thread_on_reviewer_error(self, _reload_gate):
        """A reviewer EXECUTION failure routes a loud ``reviewer.errored`` event
        to the PR's owning thread AND stays retryable (no skip-guard write) — a
        silent reviewer failure must never let a PR look unreviewed-but-fine."""
        from unittest.mock import AsyncMock

        gate_mod = _reload_gate
        delivered = AsyncMock(return_value=True)

        with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=_DEFAULT_PR_INFO), \
             patch.object(gate_mod, "emit_product_event"), \
             patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch("reviewer.on_pr_review.on_pr_review",
                   side_effect=RuntimeError("[Errno 2] No such file or directory: 'claude'")), \
             patch("events.delivery.owning_thread_for_identity",
                   return_value="owner-thread"), \
             patch("events.delivery.deliver_to_thread", delivered):
            _call_gate(gate_mod)

        # Retryable: an errored review is NOT recorded as reviewed.
        assert gate_mod.reviewer_gate_state.get_reviewed_sha("owner/repo#42") is None
        # Owner thread woken with the loud routed event.
        delivered.assert_awaited_once()
        args, _ = delivered.call_args
        assert args[0] == "owner-thread"
        assert args[1] == "reviewer.errored"


def _gh_transport_fake(
    standing: list[dict], posts: list[dict], reads: list[list[str]],
    *, read_fails: bool = False,
):
    """``subprocess.run`` fake for the ``gh`` transport.

    Serves the combined-status read (``.../commits/{sha}/status``) from
    ``standing`` — pre-filtered JSON lines, as ``gh --jq '.statuses[]'`` would
    emit — and records every status POST body into ``posts``. ``standing`` is
    read at call time, so tests may mutate it between gate retries.
    """
    def _run(cmd, **kwargs):
        proc = MagicMock()
        proc.returncode = 0
        proc.stderr = ""
        proc.stdout = ""
        if "--method" in cmd and "POST" in cmd:
            posts.append(json.loads(kwargs.get("input")))
            proc.stdout = '{"id": 1}'
        elif any(str(arg).endswith("/status") for arg in cmd):
            reads.append(list(cmd))
            if read_fails:
                proc.returncode = 1
                proc.stderr = "API rate limit exceeded"
            else:
                proc.stdout = "\n".join(json.dumps(s) for s in standing)
        return proc
    return _run


class TestInfraRedRespectsStandingSuccess:
    """An infra-error retry must never stomp a newer success another writer
    posted (incident: the gate's error-retry loop repeatedly overwrote a
    human-posted success waiver on qa/llm-reviewer until the PR merged)."""

    _GATE_ERROR_DESC = "qa/llm-reviewer: could not fetch PR metadata"

    def _retry_gate_with_unreadable_pr_info(self, gate_mod, fake):
        """Drive the gate's real infra-error post path through ``fake``."""
        from events.connectors import github_reviewer_notifications

        with patch("reviewer.gh_api.subprocess.run", side_effect=fake), \
             patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=None), \
             patch.object(
                 github_reviewer_notifications, "notify_pr_info_unreadable",
             ), \
             patch.object(gate_mod, "emit_product_event"):
            _call_gate(gate_mod)

    def test_standing_success_not_stomped_by_infra_error_retry(
        self, _reload_gate, caplog,
    ):
        """A standing success (waiver) suppresses the gate's error re-post."""
        gate_mod = _reload_gate
        standing = [{
            "context": "qa/llm-reviewer",
            "state": "success",
            "description": "waived: reviewed by hand",
        }]
        posts, reads = [], []
        fake = _gh_transport_fake(standing, posts, reads)

        with caplog.at_level(logging.WARNING, logger="reviewer.reviewer_status"):
            self._retry_gate_with_unreadable_pr_info(gate_mod, fake)
            self._retry_gate_with_unreadable_pr_info(gate_mod, fake)  # retry

        assert posts == []  # the waiver survives every retry
        assert len(reads) == 2  # each retry consulted the standing status
        assert "standing success" in caplog.text

    def test_identical_infra_error_not_reposted(self, _reload_gate):
        """An identical standing red is not re-posted; a different red is."""
        gate_mod = _reload_gate
        standing = [{
            "context": "qa/llm-reviewer",
            "state": "error",
            "description": self._GATE_ERROR_DESC,
        }]
        posts, reads = [], []
        fake = _gh_transport_fake(standing, posts, reads)

        self._retry_gate_with_unreadable_pr_info(gate_mod, fake)
        assert posts == []  # identical error already visible — no spam

        standing[0] = {
            "context": "qa/llm-reviewer",
            "state": "error",
            "description": "qa/llm-reviewer: some other earlier error",
        }
        self._retry_gate_with_unreadable_pr_info(gate_mod, fake)
        assert [p["state"] for p in posts] == ["error"]  # changed red posts
        assert posts[0]["description"].startswith(self._GATE_ERROR_DESC)
        assert STATUS_WRITE_STAMP.search(posts[0]["description"])
        assert posts[0]["context"] == "qa/llm-reviewer"

    def test_unreadable_standing_status_fails_open(self, _reload_gate):
        """When the standing status is unreadable the red signal still posts —
        losing the dedupe is safer than losing the red signal."""
        gate_mod = _reload_gate
        posts, reads = [], []
        fake = _gh_transport_fake([], posts, reads, read_fails=True)

        self._retry_gate_with_unreadable_pr_info(gate_mod, fake)

        assert len(reads) == 1
        assert [p["state"] for p in posts] == ["error"]
        assert posts[0]["description"].startswith(self._GATE_ERROR_DESC)
        assert STATUS_WRITE_STAMP.search(posts[0]["description"])

    def test_real_verdict_still_overwrites_standing_success(self):
        """A genuine code_fail verdict is senior to a success waiver placed
        before it: it posts unconditionally, without consulting the standing
        status."""
        from reviewer import on_pr_review

        standing = [{
            "context": "qa/llm-reviewer",
            "state": "success",
            "description": "waived: reviewed by hand",
        }]
        posts, reads = [], []
        fake = _gh_transport_fake(standing, posts, reads)
        findings = [{
            "severity": "block",
            "category": "correctness/logic-error",
            "file": "backend/x.py",
            "cited_section": None,
        }]

        with patch("reviewer.gh_api.subprocess.run", side_effect=fake), \
             patch.object(on_pr_review, "parse_findings", return_value=findings), \
             patch.object(
                 on_pr_review, "fetch_existing_review_comments", return_value=[],
             ), \
             patch.object(
                 on_pr_review, "fetch_existing_issue_comments", return_value=[],
             ), \
             patch.object(
                 on_pr_review, "classify_review_findings",
                 return_value=(findings, [], 0),
             ), \
             patch.object(on_pr_review, "publish_review"), \
             patch.object(on_pr_review, "emit_product_event"):
            verdict = on_pr_review._process_findings(
                "[]", 42, "owner/repo", "abc123def", "codex",
            )

        assert verdict["verdict"] == "code_fail"
        assert [p["state"] for p in posts] == ["failure"]
        assert "1 block" in posts[0]["description"]
        assert reads == []  # a verdict never depends on the standing status

    def test_infra_set_failure_default_is_guarded(self):
        """set_failure's default (verdict=False) is the guarded infra path:
        a standing success suppresses it, an identical standing failure is
        deduped, and a changed failure still posts."""
        from reviewer import reviewer_status

        standing = [{
            "context": "qa/llm-reviewer",
            "state": "success",
            "description": "waived: reviewed by hand",
        }]
        posts, reads = [], []
        fake = _gh_transport_fake(standing, posts, reads)
        infra_desc = "qa/llm-reviewer: model-unavailable — 529"

        with patch("reviewer.gh_api.subprocess.run", side_effect=fake):
            assert reviewer_status.set_failure(
                "owner/repo", "abc123def", infra_desc,
            ) is True
            assert posts == []  # standing success respected

            standing[0] = {
                "context": "qa/llm-reviewer",
                "state": "failure",
                "description": f"{infra_desc} [m:x w:a]",
            }
            assert reviewer_status.set_failure(
                "owner/repo", "abc123def", infra_desc,
            ) is True
            assert [p["state"] for p in posts] == ["failure"]  # malformed suffix is not deduped

            standing[0] = {
                "context": "qa/llm-reviewer",
                "state": "failure",
                "description": f"{infra_desc} [m:abc123def456 w:abcdef123456]",
            }
            assert reviewer_status.set_failure(
                "owner/repo", "abc123def", infra_desc,
            ) is True
            assert [p["state"] for p in posts] == ["failure"]  # exact writer stamp deduped

            assert reviewer_status.set_failure(
                "owner/repo", "abc123def", "qa/llm-reviewer: 27B-not-loaded",
            ) is True
            assert [p["state"] for p in posts] == ["failure", "failure"]  # changed red posts
            assert posts[1]["description"].startswith("qa/llm-reviewer: 27B-not-loaded")
            assert STATUS_WRITE_STAMP.search(posts[0]["description"])
        assert len(reads) == 4  # every non-verdict post consulted the guard

    def test_pending_respects_standing_success_and_review_continues(
        self, monkeypatch, caplog,
    ):
        """The gate re-fires a FULL review after an infra error: the
        in-progress pending must not stomp a standing success waiver, the
        review still runs silently, and its terminal verdict — senior to any
        waiver — posts normally at the end."""
        from reviewer import on_pr_review

        monkeypatch.setenv("CORSIA_REVIEWER_MODEL", "claude-opus-4-8")
        standing = [{
            "context": "qa/llm-reviewer",
            "state": "success",
            "description": "waived: reviewed by hand",
        }]
        posts, reads = [], []
        fake = _gh_transport_fake(standing, posts, reads)
        payload = {
            "action": "synchronize",
            "pull_request": {
                "number": 42,
                "head": {"sha": "abc123def"},
                "base": {"ref": "dev", "sha": "b" * 40},
                "labels": [],
            },
            "repository": {"full_name": "owner/repo"},
        }

        with patch("reviewer.gh_api.subprocess.run", side_effect=fake), \
             patch.object(on_pr_review, "_review_diff", return_value="+x"), \
             patch.object(on_pr_review, "_build_context", return_value="ctx"), \
             patch.object(
                 on_pr_review, "run_cc_review", return_value=("[]", "codex"),
             ) as run_review, \
             patch.object(
                 on_pr_review, "fetch_existing_review_comments", return_value=[],
             ), \
             patch.object(
                 on_pr_review, "fetch_existing_issue_comments", return_value=[],
             ), \
             patch.object(
                 on_pr_review, "classify_review_findings", return_value=([], [], 0),
             ), \
             patch.object(on_pr_review, "publish_review"), \
             patch.object(on_pr_review, "emit_product_event"), \
             caplog.at_level(logging.WARNING, logger="reviewer.reviewer_status"):
            result = on_pr_review.on_pr_review(
                "webhook.github.check_suite_completed", payload,
            )

        run_review.assert_called_once()  # the review itself still ran
        assert run_review.call_args.kwargs["base_sha"] == "b" * 40
        assert "No prior reviewer findings" in run_review.call_args.kwargs[
            "review_history"
        ]
        assert result is not None and result["verdict"] == "clean"
        # No pending post — only the terminal verdict replaced the waiver.
        assert [p["state"] for p in posts] == ["success"]
        assert "review proceeding silently" in caplog.text


def test_completed_exact_head_supplies_thread_state_for_resolution():
    from reviewer import on_pr_review
    from reviewer.review_comment_fetch import (
        ReviewThreadState, ReviewThreadsSnapshot,
    )
    from reviewer.reviewer_threads import (
        THREAD_ACTION_KEY, format_threaded_comment_body,
    )

    head = "a" * 40
    root = {
        "id": 701,
        "body": format_threaded_comment_body({
            "severity": "block", "category": "correctness/logic-error",
            "file": "backend/x.py", "line": 8, "description": "Broken.",
        }),
        "path": "backend/x.py", "line": 8,
        "original_line": 8, "original_commit_id": head,
        "pull_request_url": "https://api.github.com/repos/owner/repo/pulls/42",
    }
    snapshot = ReviewThreadsSnapshot(
        head, {701: ReviewThreadState("PRRT_701", False)},
    )

    with patch.object(on_pr_review, "parse_findings", return_value=[]), \
         patch.object(on_pr_review, "fetch_existing_review_comments", return_value=[root]), \
         patch.object(on_pr_review, "fetch_existing_issue_comments", return_value=[]), \
         patch("reviewer.review_thread_lifecycle.fetch_existing_review_threads", return_value=snapshot), \
         patch.object(on_pr_review, "publish_review") as publish_review, \
         patch.object(on_pr_review, "set_success", return_value=True), \
         patch.object(on_pr_review, "emit_product_event"):
        on_pr_review._process_findings("[]", 42, "owner/repo", head, "claude")

    resolutions = publish_review.call_args.kwargs["resolve_replies"]
    assert len(resolutions) == 1
    assert resolutions[0][THREAD_ACTION_KEY].thread_id == "PRRT_701"


def test_stale_thread_snapshot_does_not_enable_lifecycle_mutations():
    from reviewer import on_pr_review
    from reviewer.review_comment_fetch import (
        ReviewThreadState, ReviewThreadsSnapshot,
    )
    from reviewer.reviewer_threads import format_threaded_comment_body

    root = {
        "id": 702,
        "body": format_threaded_comment_body({
            "severity": "flag", "category": "correctness/logic-error",
            "file": "backend/x.py", "line": 9, "description": "Check this.",
        }),
        "path": "backend/x.py", "line": 9,
        "pull_request_url": "https://api.github.com/repos/owner/repo/pulls/42",
    }
    snapshot = ReviewThreadsSnapshot(
        "b" * 40, {702: ReviewThreadState("PRRT_702", False)},
    )

    with patch.object(on_pr_review, "parse_findings", return_value=[]), \
         patch.object(on_pr_review, "fetch_existing_review_comments", return_value=[root]), \
         patch.object(on_pr_review, "fetch_existing_issue_comments", return_value=[]), \
         patch("reviewer.review_thread_lifecycle.fetch_existing_review_threads", return_value=snapshot), \
         patch.object(on_pr_review, "publish_review") as publish_review, \
         patch.object(on_pr_review, "set_success", return_value=True), \
         patch("reviewer.review_thread_lifecycle.emit_product_event") as emit_event:
        on_pr_review._process_findings("[]", 42, "owner/repo", "a" * 40, "claude")

    assert publish_review.call_args.kwargs["resolve_replies"] == []
    assert any(
        call.args[1] == "thread_lifecycle"
        and call.kwargs["properties"]["outcome"] == "stale_head"
        for call in emit_event.call_args_list
    )
