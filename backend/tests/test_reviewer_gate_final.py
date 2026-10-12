from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import ANY, patch

import pytest
from events.connectors import github_reviewer_notifications
from reviewer_gate_fixtures import (  # noqa: F401
    _DEFAULT_PR_INFO,
    _call_on_backend_loop,
    _call_gate,
    _check_suite_payload,
    _disable_analytics,
    _reload_gate,
)


class TestFinalGate:
    def test_gate_holds_until_all_non_reviewer_checks_are_green(self, _reload_gate):
        gate_mod = _reload_gate

        with patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=False), \
             patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info") as mock_fetch, \
             patch.object(gate_mod, "_run_reviewer") as mock_run, \
             patch.object(gate_mod, "emit_product_event"):

            gate_mod.on_check_suite_completed(
                repo="owner/repo", head_sha="abc123def",
                conclusion="success", pr_number=42,
                internal_event="webhook.github.check_suite_completed",
            )

        mock_fetch.assert_not_called()
        mock_run.assert_not_called()

    def test_final_suite_is_not_lost_while_an_earlier_suite_reads_checks(self, _reload_gate):
        gate_mod = _reload_gate
        first_reading = threading.Event()
        release_first = threading.Event()
        pr_key = "owner/repo#42"

        def checks_green(_repo, _pr_number, _head_sha):
            if threading.current_thread().name == "earlier-suite":
                first_reading.set()
                assert release_first.wait(timeout=5)
                return False
            return True

        def complete_suite():
            gate_mod.on_check_suite_completed(
                repo="owner/repo", head_sha="abc123def", conclusion="success", pr_number=42,
                internal_event="webhook.github.check_suite_completed",
            )

        def mark_reviewed(*_args, **_kwargs):
            gate_mod.reviewer_gate_state.mark_reviewed(pr_key, "abc123def")

        def complete_earlier_suite():
            threading.current_thread().name = "earlier-suite"
            complete_suite()

        with patch.object(gate_mod.github_reviewer_gate, "checks_green", side_effect=checks_green), \
             patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=_DEFAULT_PR_INFO), \
             patch.object(gate_mod, "_run_reviewer", side_effect=mark_reviewed) as review, \
             patch.object(gate_mod, "emit_product_event"):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(complete_earlier_suite)
                assert first_reading.wait(timeout=5)
                try:
                    executor.submit(complete_suite).result(timeout=5)
                finally:
                    release_first.set()
                first.result(timeout=5)

        review.assert_called_once()
        assert gate_mod.reviewer_gate_state.get_reviewed_sha(pr_key) == "abc123def"

    def test_receipt_is_rechecked_after_claim_when_eligibility_reads_overlap(self, _reload_gate):
        gate_mod = _reload_gate
        second_fetching = threading.Event()
        release_second = threading.Event()
        pr_key = "owner/repo#42"

        def fetch_pr_info(_repo, _pr_number):
            if threading.current_thread().name == "second-trigger":
                second_fetching.set()
                assert release_second.wait(timeout=5)
            return _DEFAULT_PR_INFO

        def complete_suite():
            gate_mod.on_check_suite_completed(
                repo="owner/repo", head_sha="abc123def", conclusion="success", pr_number=42,
                internal_event="webhook.github.check_suite_completed",
            )

        def second_trigger():
            threading.current_thread().name = "second-trigger"
            complete_suite()

        def mark_reviewed(*_args, **_kwargs):
            gate_mod.reviewer_gate_state.mark_reviewed(pr_key, "abc123def")

        with patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", side_effect=fetch_pr_info), \
             patch.object(gate_mod, "_run_reviewer", side_effect=mark_reviewed) as review, \
             patch.object(gate_mod, "emit_product_event"):
            with ThreadPoolExecutor(max_workers=2) as executor:
                second = executor.submit(second_trigger)
                assert second_fetching.wait(timeout=5)
                try:
                    complete_suite()
                finally:
                    release_second.set()
                second.result(timeout=5)

        review.assert_called_once()
        assert gate_mod.reviewer_gate_state.get_reviewed_sha(pr_key) == "abc123def"

    def test_gate_skips_duplicate_in_flight_sha(self, _reload_gate):
        gate_mod = _reload_gate

        assert gate_mod.reviewer_gate_state.claim_in_flight("owner/repo#42", "abc123def") is True
        try:
            with patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
                 patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=_DEFAULT_PR_INFO) as mock_fetch, \
                 patch.object(gate_mod, "_run_reviewer") as mock_run, \
                 patch.object(gate_mod, "emit_product_event"):

                gate_mod.on_check_suite_completed(
                    repo="owner/repo", head_sha="abc123def",
                    conclusion="success", pr_number=42,
                    internal_event="webhook.github.check_suite_completed",
                )
        finally:
            gate_mod.reviewer_gate_state.release_in_flight("owner/repo#42", "abc123def")

        mock_fetch.assert_called_once()
        mock_run.assert_not_called()

    @pytest.mark.parametrize("ready_checks_green", [True, False])
    def test_ready_event_evaluates_while_draft_check_is_waiting(
        self, _reload_gate, ready_checks_green,
    ):
        gate_mod = _reload_gate
        draft_seen = threading.Event()
        release_draft = threading.Event()
        draft_pr_info = {**_DEFAULT_PR_INFO, "isDraft": True}

        def fetch_pr_info(_repo, _pr_number):
            if threading.current_thread().name == "draft-check":
                draft_seen.set()
                assert release_draft.wait(timeout=5)
                return draft_pr_info
            return _DEFAULT_PR_INFO

        def checks_green(_repo, _pr_number, _head_sha):
            if threading.current_thread().name == "draft-check":
                return True
            return ready_checks_green

        def check_suite():
            threading.current_thread().name = "draft-check"
            gate_mod.on_check_suite_completed(
                repo="owner/repo", head_sha="abc123def", conclusion="success", pr_number=42,
                internal_event="webhook.github.check_suite_completed",
            )

        def ready_transition():
            gate_mod.on_pull_request_ready_for_review(
                repo="owner/repo", head_sha="abc123def", pr_number=42,
                internal_event="webhook.github.pr_ready_for_review",
            )

        def mark_reviewed(*_args, **_kwargs):
            gate_mod.reviewer_gate_state.mark_reviewed("owner/repo#42", "abc123def")

        with patch.object(gate_mod.github_reviewer_gate, "checks_green", side_effect=checks_green), \
             patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", side_effect=fetch_pr_info), \
             patch.object(gate_mod, "_run_reviewer", side_effect=mark_reviewed) as run_review, \
             patch.object(gate_mod, "emit_product_event"):
            with ThreadPoolExecutor(max_workers=2) as executor:
                draft_future = executor.submit(check_suite)
                assert draft_seen.wait(timeout=5)
                try:
                    executor.submit(ready_transition).result(timeout=0.5)
                    assert not release_draft.is_set()
                finally:
                    release_draft.set()
                draft_future.result(timeout=5)

        assert run_review.call_count == int(ready_checks_green)
        assert gate_mod.reviewer_gate_state.get_reviewed_sha("owner/repo#42") == (
            "abc123def" if ready_checks_green else None
        )

    def test_repeated_ready_events_return_while_review_runs(self, _reload_gate):
        gate_mod = _reload_gate
        review_started = threading.Event()
        finish_review = threading.Event()
        pr_key = "owner/repo#42"

        def run_reviewer(*_args, **_kwargs):
            review_started.set()
            assert finish_review.wait(timeout=5)
            gate_mod.reviewer_gate_state.mark_reviewed(pr_key, "abc123def")

        def check_suite():
            gate_mod.on_check_suite_completed(
                repo="owner/repo", head_sha="abc123def", conclusion="success", pr_number=42,
                internal_event="webhook.github.check_suite_completed",
            )

        def ready_transition():
            gate_mod.on_pull_request_ready_for_review(
                repo="owner/repo", head_sha="abc123def", pr_number=42,
                internal_event="webhook.github.pr_ready_for_review",
            )

        with patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=_DEFAULT_PR_INFO), \
             patch.object(gate_mod, "_run_reviewer", side_effect=run_reviewer) as review, \
             patch.object(gate_mod, "emit_product_event"):
            with ThreadPoolExecutor(max_workers=2) as executor:
                review_future = executor.submit(check_suite)
                assert review_started.wait(timeout=5)
                try:
                    for _ in range(5):
                        executor.submit(ready_transition).result(timeout=0.5)
                finally:
                    finish_review.set()
                review_future.result(timeout=5)

        review.assert_called_once()
        assert gate_mod.reviewer_gate_state.get_reviewed_sha(pr_key) == "abc123def"

    def test_gate_runs_reviewer_without_auto_merge(self, _reload_gate):
        gate_mod = _reload_gate
        calls = []

        assert not hasattr(gate_mod, "_auto_merge")

        with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=_DEFAULT_PR_INFO), \
             patch.object(gate_mod, "emit_product_event"), \
             patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch("reviewer.on_pr_review.on_pr_review",
                   side_effect=lambda *a, **kw: calls.append(a)):

            gate_mod.on_check_suite_completed(
                repo="owner/repo", head_sha="abc123def",
                conclusion="success", pr_number=42,
                internal_event="webhook.github.check_suite_completed",
            )

        assert calls

    def test_gate_holds_for_draft_pr_without_running_reviewer(self, _reload_gate):
        gate_mod = _reload_gate
        emitted = []
        draft_pr_info = {
            **_DEFAULT_PR_INFO, "isDraft": True, "mergeable": "UNKNOWN",
        }

        with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=draft_pr_info) as mock_fetch, \
             patch.object(gate_mod.github_reviewer_gate, "MERGEABILITY_POLL_INTERVAL_S", 0.0), \
             patch("infra.product_events.emit_product_event",
                   side_effect=lambda s, e, **kw: emitted.append((s, e))), \
             patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch.object(gate_mod, "_run_reviewer") as mock_run:
            gate_mod.on_check_suite_completed(
                repo="owner/repo", head_sha="abc123def",
                conclusion="success", pr_number=42,
                internal_event="webhook.github.check_suite_completed",
            )

        mock_run.assert_not_called()
        mock_fetch.assert_called_once()
        assert ("reviewer_gate", "draft_hold") in emitted

    def test_draft_then_ready_reviews_once_and_replay_skips(self, _reload_gate):
        gate_mod = _reload_gate
        pr_key = "owner/repo#42"
        draft_pr_info = {**_DEFAULT_PR_INFO, "isDraft": True}

        def _mark_ready_review(*_args, **_kwargs):
            gate_mod.reviewer_gate_state.mark_reviewed(pr_key, "abc123def")

        with patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True) as checks, \
             patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", side_effect=[draft_pr_info, _DEFAULT_PR_INFO]), \
             patch.object(gate_mod, "emit_product_event"), \
             patch.object(gate_mod, "_run_reviewer", side_effect=_mark_ready_review) as mock_run:
            gate_mod.on_check_suite_completed(
                repo="owner/repo", head_sha="abc123def", conclusion="success", pr_number=42,
                internal_event="webhook.github.check_suite_completed",
            )
            assert gate_mod.reviewer_gate_state.get_reviewed_sha(pr_key) is None
            gate_mod.on_pull_request_ready_for_review(
                repo="owner/repo", head_sha="abc123def", pr_number=42,
                internal_event="webhook.github.pr_ready_for_review",
            )
            assert gate_mod.reviewer_gate_state.get_reviewed_sha(pr_key) == "abc123def"
            gate_mod.on_pull_request_ready_for_review(
                repo="owner/repo", head_sha="abc123def", pr_number=42,
                internal_event="webhook.github.pr_ready_for_review",
            )

        mock_run.assert_called_once()
        assert checks.call_count == 2

    def test_ready_does_not_review_a_superseded_head(self, _reload_gate):
        gate_mod = _reload_gate
        stale_pr_info = {**_DEFAULT_PR_INFO, "headRefOid": "newhead123"}

        with patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=stale_pr_info), \
             patch.object(gate_mod, "_run_reviewer") as run_review:
            gate_mod.on_pull_request_ready_for_review(
                repo="owner/repo", head_sha="abc123def", pr_number=42,
                internal_event="webhook.github.pr_ready_for_review",
            )

        run_review.assert_not_called()
        assert gate_mod.reviewer_gate_state.get_reviewed_sha("owner/repo#42") is None

    def test_ready_preserves_existing_exact_head_review(self, _reload_gate):
        gate_mod = _reload_gate
        pr_key = "owner/repo#42"
        gate_mod.reviewer_gate_state.mark_reviewed(pr_key, "abc123def")

        with patch.object(gate_mod.github_reviewer_gate, "checks_green") as checks, \
             patch.object(gate_mod, "_run_reviewer") as mock_run:
            gate_mod.on_pull_request_ready_for_review(
                repo="owner/repo", head_sha="abc123def", pr_number=42,
                internal_event="webhook.github.pr_ready_for_review",
            )

        checks.assert_not_called()
        mock_run.assert_not_called()
        assert gate_mod.reviewer_gate_state.get_reviewed_sha(pr_key) == "abc123def"

    def test_ready_waits_for_green_ci_then_later_check_runs(self, _reload_gate):
        gate_mod = _reload_gate
        pr_key = "owner/repo#42"

        def _mark_ready_review(*_args, **_kwargs):
            gate_mod.reviewer_gate_state.mark_reviewed(pr_key, "abc123def")

        with patch.object(gate_mod.github_reviewer_gate, "checks_green", side_effect=[False, True]), \
             patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=_DEFAULT_PR_INFO), \
             patch.object(gate_mod, "emit_product_event"), \
             patch.object(gate_mod, "_run_reviewer", side_effect=_mark_ready_review) as mock_run:
            gate_mod.on_pull_request_ready_for_review(
                repo="owner/repo", head_sha="abc123def", pr_number=42,
                internal_event="webhook.github.pr_ready_for_review",
            )
            mock_run.assert_not_called()
            assert gate_mod.reviewer_gate_state.get_reviewed_sha(pr_key) is None
            gate_mod.on_check_suite_completed(
                repo="owner/repo", head_sha="abc123def",
                conclusion="success", pr_number=42,
                internal_event="webhook.github.check_suite_completed",
            )

        mock_run.assert_called_once()
        assert gate_mod.reviewer_gate_state.get_reviewed_sha(pr_key) == "abc123def"

    def test_no_auto_merge_when_pr_not_mergeable(self, _reload_gate):
        gate_mod = _reload_gate
        run_reviewer = []

        conflict_pr_info = {**_DEFAULT_PR_INFO, "mergeable": "CONFLICTING"}

        with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=conflict_pr_info), \
             patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch.object(gate_mod, "emit_product_event"), \
             patch.object(gate_mod, "_run_reviewer",
                          side_effect=lambda *a, **kw: run_reviewer.append(1)):

            _call_gate(gate_mod)

        assert run_reviewer == []

    def test_checks_green_ignores_reviewer_and_requires_other_checks_green(
        self, _reload_gate
    ):
        gate_mod = _reload_gate
        # REST check-runs shape: conclusion/status mapped to uppercase state
        checks = [
            {"name": "Backend unit tests", "state": "SUCCESS"},
            {"name": "qa/llm-reviewer", "state": "PENDING"},
            {"name": "PR format check (advisory)", "state": "SKIPPED"},
        ]
        with patch("reviewer.gh_helpers.get_check_runs_for_sha", return_value=checks):
            assert gate_mod.github_reviewer_gate.checks_green("owner/repo", 42, "abc123def") is True

    def test_checks_green_uses_newest_duplicate_run_for_each_name(self, _reload_gate):
        """Cancelled concurrency copies cannot veto newer successful copies."""
        gate_mod = _reload_gate
        checks = "\n".join([
            json.dumps({"id": 1, "name": "unit", "conclusion": "cancelled", "completed_at": "2026-09-29T04:07:00Z"}),
            json.dumps({"id": 2, "name": "unit", "conclusion": "success", "completed_at": "2026-09-29T04:14:00Z"}),
            json.dumps({"id": 3, "name": "lint", "conclusion": "cancelled", "completed_at": "2026-09-29T04:07:00Z"}),
            json.dumps({"id": 4, "name": "lint", "conclusion": "success", "completed_at": "2026-09-29T04:14:00Z"}),
        ])
        with patch("reviewer.gh_helpers._run_gh", return_value=checks):
            assert gate_mod.github_reviewer_gate.checks_green("owner/repo", 42, "abc123def") is True

    def test_checks_green_holds_when_newest_run_is_cancelled(self, _reload_gate):
        """A name with no successful replacement remains a gate hold."""
        gate_mod = _reload_gate
        checks = json.dumps({
            "id": 1, "name": "unit", "conclusion": "cancelled",
            "completed_at": "2026-09-29T04:14:00Z",
        })
        with patch("reviewer.gh_helpers._run_gh", return_value=checks):
            assert gate_mod.github_reviewer_gate.checks_green("owner/repo", 42, "abc123def") is False

    def test_checks_green_holds_when_newest_run_failed(self, _reload_gate):
        """A newer failure supersedes an older success and holds the gate."""
        gate_mod = _reload_gate
        checks = "\n".join([
            json.dumps({"id": 1, "name": "unit", "conclusion": "success", "completed_at": "2026-09-29T04:07:00Z"}),
            json.dumps({"id": 2, "name": "unit", "conclusion": "failure", "completed_at": "2026-09-29T04:14:00Z"}),
        ])
        with patch("reviewer.gh_helpers._run_gh", return_value=checks):
            assert gate_mod.github_reviewer_gate.checks_green("owner/repo", 42, "abc123def") is False

    def test_checks_green_blocks_on_any_non_reviewer_pending(self, _reload_gate):
        gate_mod = _reload_gate
        checks = [
            {"name": "Backend unit tests", "state": "SUCCESS"},
            {"name": "Frontend unit tests", "state": "PENDING"},
            {"name": "qa/llm-reviewer", "state": "PENDING"},
        ]
        with patch("reviewer.gh_helpers.get_check_runs_for_sha", return_value=checks):
            assert gate_mod.github_reviewer_gate.checks_green("owner/repo", 42, "abc123def") is False

    def test_checks_green_returns_none_when_fetch_unreadable(self, _reload_gate):
        """Unreadable check state returns None — distinct from False — so the
        caller can emit a loud retryable event instead of silently holding."""
        gate_mod = _reload_gate
        with patch("reviewer.gh_helpers.get_check_runs_for_sha", return_value=None):
            assert gate_mod.github_reviewer_gate.checks_green("owner/repo", 42, "abc123def") is None


class TestMergeableGate:
    def test_repeated_unresolved_mergeability_repeats_recovery_wake(self, _reload_gate):
        """Each exhausted retry cycle wakes the owner, even with a stale red status."""
        gate_mod = _reload_gate
        unresolved = {**_DEFAULT_PR_INFO, "mergeable": "UNKNOWN"}
        standing_failure = {
            "state": "error",
            "description": "qa/llm-reviewer: a prior unrelated error",
        }
        reason = "mergeability remained unresolved after retries"

        with patch.object(
            gate_mod.github_reviewer_gate,
            "fetch_pr_info_awaiting_mergeability",
            return_value=unresolved,
        ), patch.object(
            gate_mod, "emit_product_event",
        ) as emit_event, patch.object(
            github_reviewer_notifications, "notify_reviewer_terminal_error",
        ) as notify_error, patch(
            "reviewer.reviewer_status.get_commit_status_for_context",
            return_value=standing_failure,
        ), patch("reviewer.reviewer_status.set_error") as set_error:
            for _ in range(2):
                gate_mod._evaluate_ready_pr(
                    "owner/repo",
                    42,
                    "abc123def",
                    "webhook.github.check_suite_completed",
                    mergeability_retry_attempt=len(gate_mod.MERGEABILITY_RETRY_DELAYS_S),
                )

        assert set_error.call_count == 2
        assert set_error.call_args.args == (
            "owner/repo",
            "abc123def",
            f"qa/llm-reviewer: {reason}; retry required",
        )
        assert notify_error.call_count == 2
        assert notify_error.call_args.args == (
            "owner/repo", 42, "abc123def", reason, None,
        )
        assert sum(
            call.args[:2] == ("reviewer_gate", "mergeability_unresolved")
            for call in emit_event.call_args_list
        ) == 2

    def test_gate_holds_on_real_conflict_without_retry(self, _reload_gate):
        """CONFLICTING is terminal — hold immediately, do not re-poll."""
        gate_mod = _reload_gate
        conflict = {**_DEFAULT_PR_INFO, "mergeable": "CONFLICTING"}

        with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=conflict) as mock_fetch, \
             patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch.object(gate_mod, "_run_reviewer") as mock_run, \
             patch.object(gate_mod, "emit_product_event"):

            _call_gate(gate_mod)

        mock_run.assert_not_called()
        mock_fetch.assert_called_once()  # no retry on a real conflict

    @pytest.mark.parametrize("mergeable", ["UNKNOWN", None])
    def test_gate_holds_when_mergeability_never_settles(self, _reload_gate, mergeable):
        """A mergeable that stays UNKNOWN past the budget falls through to hold."""
        gate_mod = _reload_gate
        pr_info = {**_DEFAULT_PR_INFO, "mergeable": mergeable}

        with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=pr_info) as mock_fetch, \
             patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch.object(gate_mod.github_reviewer_gate, "MERGEABILITY_POLL_INTERVAL_S", 0.0), \
             patch.object(gate_mod, "_run_reviewer") as mock_run, \
             patch.object(gate_mod, "emit_product_event"):

            _call_gate(gate_mod)

        mock_run.assert_not_called()
        # UNKNOWN is retried up to the full budget before giving up (BUG #574).
        assert mock_fetch.call_count == gate_mod.github_reviewer_gate.MERGEABILITY_POLL_ATTEMPTS

    def test_gate_fires_after_transient_unknown_mergeability(self, _reload_gate):
        """The #574 fix: UNKNOWN that settles to MERGEABLE fires the reviewer."""
        gate_mod = _reload_gate
        unknown = {**_DEFAULT_PR_INFO, "mergeable": "UNKNOWN"}
        ready = {**_DEFAULT_PR_INFO, "mergeable": "MERGEABLE"}
        run_reviewer = []

        with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info",
                          side_effect=[unknown, unknown, ready]) as mock_fetch, \
             patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch.object(gate_mod.github_reviewer_gate, "MERGEABILITY_POLL_INTERVAL_S", 0.0), \
             patch.object(gate_mod, "_run_reviewer",
                          side_effect=lambda *a, **kw: run_reviewer.append(1)), \
             patch.object(gate_mod, "emit_product_event"):

            _call_gate(gate_mod)

        assert run_reviewer == [1]
        assert mock_fetch.call_count == 3  # re-polled until mergeability settled

    def test_gate_holds_when_gh_returns_none(self, _reload_gate):
        gate_mod = _reload_gate

        with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=None), \
             patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch.object(gate_mod, "_run_reviewer") as mock_run, \
             patch.object(gate_mod, "emit_product_event"):

            _call_gate(gate_mod)

        mock_run.assert_not_called()

    def test_gate_emits_pr_info_unreadable_when_fetch_fails(self, _reload_gate):
        """A green-check edge with unreadable PR info must stay loud/retryable.

        This covers the GraphQL quota failure mode where checks are readable via
        REST, but PR metadata/mergeability cannot be fetched. The gate must not
        quietly return because the terminal check-suite edge may be the last
        natural trigger for that PR head.
        """
        gate_mod = _reload_gate
        emitted = []

        with patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=None), \
             patch.object(gate_mod, "_run_reviewer") as mock_run, \
             patch.object(
                 github_reviewer_notifications, "notify_pr_info_unreadable",
             ) as mock_notify, \
             patch("reviewer.reviewer_status.set_error") as mock_status, \
             patch.object(gate_mod, "emit_product_event",
                          side_effect=lambda s, e, **kw: emitted.append((s, e))):

            _call_gate(gate_mod)

        mock_run.assert_not_called()
        mock_notify.assert_called_once_with("owner/repo", 42, "abc123def", ANY)
        mock_status.assert_called_once_with(
            "owner/repo",
            "abc123def",
            "qa/llm-reviewer: could not fetch PR metadata",
        )
        assert ("reviewer_gate", "pr_info_unreadable") in emitted
        assert gate_mod.reviewer_gate_state.get_reviewed_sha("owner/repo#42") is None

    def test_fetch_pr_info_uses_rest_not_graphql(self, _reload_gate):
        """PR metadata comes from REST ``gh api``, not GraphQL ``gh pr view``."""
        gate_mod = _reload_gate
        rest_payload = {
            "number": 42,
            "mergeable": True,
            "draft": True,
            "base": {"ref": "dev", "sha": "base123def"},
            "head": {"sha": "abc123def"},
            "labels": [{"name": "ready"}],
            "title": "Contain reviewer scope",
            "body": "## Scope\nOnly review the changed release slice.",
        }

        with patch("reviewer.gh_helpers._run_gh",
                   return_value=json.dumps(rest_payload)) as mock_gh:
            pr_info = gate_mod.github_reviewer_gate.fetch_pr_info("owner/repo", 42)

        assert pr_info == {
            "number": 42,
            "mergeable": "MERGEABLE",
            "isDraft": True,
            "baseRefName": "dev",
            "baseRefOid": "base123def",
            "headRefOid": "abc123def",
            "labels": [{"name": "ready"}],
            "title": "Contain reviewer scope",
            "body": "## Scope\nOnly review the changed release slice.",
        }
        mock_gh.assert_called_once_with(["api", "repos/owner/repo/pulls/42"])

    def test_fetch_pr_info_rejects_missing_draft_state(self, _reload_gate):
        gate_mod = _reload_gate
        rest_payload = {
            "number": 42,
            "mergeable": True,
            "base": {"ref": "dev", "sha": "base123def"},
            "head": {"sha": "abc123def"},
        }

        with patch("reviewer.gh_helpers._run_gh", return_value=json.dumps(rest_payload)):
            assert gate_mod.github_reviewer_gate.fetch_pr_info("owner/repo", 42) is None

    def test_fetch_pr_info_rejects_a_missing_exact_base_sha(self, _reload_gate):
        gate_mod = _reload_gate
        rest_payload = {
            "number": 42,
            "mergeable": True,
            "base": {"ref": "dev"},
            "head": {"sha": "abc123def"},
        }

        with patch(
            "reviewer.gh_helpers._run_gh", return_value=json.dumps(rest_payload),
        ):
            assert gate_mod.github_reviewer_gate.fetch_pr_info("owner/repo", 42) is None


class TestIngressGateTriggerLeg:
    @pytest.mark.asyncio
    async def test_gate_verdict_delivery_uses_the_ingress_event_loop(self, _reload_gate):
        """A CI worker must not create a second loop for thread delivery."""
        from unittest.mock import AsyncMock

        from events.ingress import handle_event

        gate_mod = _reload_gate
        ingress_loop = asyncio.get_running_loop()
        delivery_loops = []
        delivered = AsyncMock(
            side_effect=lambda *args, **kwargs: delivery_loops.append(
                asyncio.get_running_loop()
            ) or True
        )
        verdict = {
            "verdict": "clean",
            "summary": "0 block, 0 flag, 0 nit -- reviewed by codex",
            "block_count": 0,
            "flag_count": 0,
            "nit_count": 0,
            "finding_count": 0,
            "model_label": "codex",
        }

        with patch("events.ingress.route_leg", new=AsyncMock(return_value=None)), \
             patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=_DEFAULT_PR_INFO), \
             patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch.object(gate_mod, "emit_product_event"), \
             patch("reviewer.on_pr_review.on_pr_review", return_value=verdict), \
             patch("events.delivery.owning_thread_for_identity", return_value="owner-thread"), \
             patch("events.delivery.deliver_to_thread", delivered):
            await handle_event(
                "webhook.github.check_suite_completed", _check_suite_payload()
            )

        assert delivery_loops == [ingress_loop]

    def test_gate_trigger_leg_dispatches_check_suite(self):
        from events.ingress import gate_trigger_leg
        from events.reviewer_route import GATE_TRIGGER_EVENTS

        assert "webhook.github.check_suite_completed" in GATE_TRIGGER_EVENTS

        payload = _check_suite_payload()
        with patch("events.reviewer_gate.on_check_suite_completed") as mock_gate:
            result = gate_trigger_leg("webhook.github.check_suite_completed", payload)

        assert result is True
        mock_gate.assert_called_once_with(
            repo="owner/repo",
            head_sha="abc123def",
            conclusion="success",
            pr_number=42,
            internal_event="webhook.github.check_suite_completed",
            loop=None,
        )

    def test_gate_trigger_leg_dispatches_ready_for_review(self):
        from events.ingress import gate_trigger_leg
        from events.reviewer_route import GATE_TRIGGER_EVENTS

        assert "webhook.github.pr_ready_for_review" in GATE_TRIGGER_EVENTS
        payload = {
            "action": "ready_for_review",
            "pull_request": {"number": 42, "head": {"sha": "abc123def"}},
            "repository": {"full_name": "owner/repo"},
        }
        with patch(
            "events.reviewer_gate.on_pull_request_ready_for_review"
        ) as mock_gate:
            result = gate_trigger_leg("webhook.github.pr_ready_for_review", payload)

        assert result is True
        mock_gate.assert_called_once_with(
            repo="owner/repo",
            head_sha="abc123def",
            pr_number=42,
            internal_event="webhook.github.pr_ready_for_review",
            loop=None,
        )

    def test_invalid_ready_event_reports_missing_identity(self):
        from events.ingress import gate_trigger_leg

        payload = {
            "action": "ready_for_review",
            "pull_request": {"number": 42, "head": {}},
            "repository": {"full_name": "owner/repo"},
        }
        with patch("events.reviewer_route.emit_product_event") as mock_event, \
             patch(
                 "events.reviewer_gate.on_pull_request_ready_for_review"
             ) as mock_gate:
            result = gate_trigger_leg("webhook.github.pr_ready_for_review", payload)

        assert result is False
        mock_gate.assert_not_called()
        mock_event.assert_called_once()
        assert mock_event.call_args.args[:2] == (
            "reviewer_gate", "ready_event_invalid",
        )
        assert mock_event.call_args.kwargs["properties"]["missing_fields"] == [
            "pull_request.head.sha",
        ]

    def test_review_leg_not_called_for_check_suite(self):
        from events.ingress import review_leg
        from events.reviewer_route import REVIEWER_EVENTS

        assert "webhook.github.check_suite_completed" not in REVIEWER_EVENTS

        result = review_leg("webhook.github.check_suite_completed", _check_suite_payload())
        assert result is False

    def test_gate_trigger_leg_ignores_non_ci_events(self):
        from events.ingress import gate_trigger_leg

        pr_push_payload = {
            "action": "synchronize",
            "pull_request": {"number": 42, "head": {"sha": "abc"}, "labels": []},
            "repository": {"full_name": "owner/repo"},
        }

        with patch("events.reviewer_gate.on_check_suite_completed") as mock_gate:
            result = gate_trigger_leg("webhook.github.pr_synchronize", pr_push_payload)

        assert result is False
        mock_gate.assert_not_called()

    def test_gate_trigger_leg_skips_missing_pr_number(self):
        from events.ingress import gate_trigger_leg

        payload = {
            "action": "completed",
            "check_suite": {
                "conclusion": "success",
                "head_sha": "abc123",
                "pull_requests": [],
            },
            "repository": {"full_name": "owner/repo"},
        }

        with patch("events.reviewer_gate.on_check_suite_completed") as mock_gate:
            result = gate_trigger_leg("webhook.github.check_suite_completed", payload)

        mock_gate.assert_not_called()


class TestChecksUnreadableGate:
    """BUG #574/#608: unreadable check state (e.g. GraphQL rate-limit) must NOT
    silently strand a green PR — the gate must emit a loud retryable event and
    wake the owning thread instead of quietly returning."""

    def test_gate_emits_checks_unreadable_event_when_fetch_fails(self, _reload_gate):
        """check_suite.completed success + REST fetch returns None → emits
        checks_unreadable (not checks_not_green) and does NOT fire the reviewer."""
        gate_mod = _reload_gate
        emitted = []

        with patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=None), \
             patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info") as mock_fetch, \
             patch.object(gate_mod, "_run_reviewer") as mock_run, \
             patch.object(github_reviewer_notifications, "notify_checks_unreadable"), \
             patch.object(gate_mod, "emit_product_event",
                          side_effect=lambda s, e, **kw: emitted.append((s, e))):

            gate_mod.on_check_suite_completed(
                repo="owner/repo", head_sha="abc123def",
                conclusion="success", pr_number=42,
                internal_event="webhook.github.check_suite_completed",
            )

        mock_fetch.assert_not_called()
        mock_run.assert_not_called()
        assert ("reviewer_gate", "checks_unreadable") in emitted
        assert ("reviewer_gate", "checks_not_green") not in emitted

    def test_gate_notifies_owner_when_checks_unreadable(self, _reload_gate):
        """Owning thread is woken with a loud event — not silently stranded."""
        from unittest.mock import AsyncMock

        gate_mod = _reload_gate
        delivered = AsyncMock(return_value=True)

        with patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=None), \
             patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info"), \
             patch.object(gate_mod, "_run_reviewer"), \
             patch.object(gate_mod, "emit_product_event"), \
             patch("events.delivery.owning_thread_for_identity",
                   return_value="owner-thread"), \
             patch("events.delivery.deliver_to_thread", delivered):
            _call_gate(gate_mod)

        delivered.assert_awaited_once()
        args, _ = delivered.call_args
        assert args[0] == "owner-thread"
        assert args[1] == "reviewer.checks_unreadable"

        summary = args[2]
        assert "retry the existing CI run for this head" in summary
        assert "do not push code or an empty/no-op commit to retrigger" in summary

    def test_pr_info_unreadable_notification_guides_no_push_retry(
        self, _reload_gate
    ):
        """PR metadata recovery retries the existing run without code churn."""
        from unittest.mock import AsyncMock

        delivered = AsyncMock(return_value=True)

        with patch("events.delivery.owning_thread_for_identity",
                   return_value="owner-thread"), \
             patch("events.delivery.deliver_to_thread", delivered):
            _call_on_backend_loop(
                github_reviewer_notifications.notify_pr_info_unreadable,
                "owner/repo", 42, "abc123def",
            )

        delivered.assert_awaited_once()
        args, _ = delivered.call_args
        assert args[0] == "owner-thread"
        assert args[1] == "reviewer.pr_info_unreadable"

        summary = args[2]
        assert "retry the existing CI run for this head" in summary
        assert "do not push code or an empty/no-op commit to retrigger" in summary

    def test_terminal_error_notification_gives_current_head_refire_instructions(
        self, _reload_gate
    ):
        """The owner receives the reason, SHA guard, and no-push recovery path."""
        from unittest.mock import AsyncMock

        delivered = AsyncMock(return_value=True)

        with patch("events.delivery.owning_thread_for_identity", return_value="owner-thread"), \
             patch("events.delivery.deliver_to_thread", delivered):
            _call_on_backend_loop(
                github_reviewer_notifications.notify_reviewer_terminal_error,
                "owner/repo", 42, "abc123def", "mergeability remained unresolved after retries",
            )

        delivered.assert_awaited_once()
        args, _ = delivered.call_args
        assert args[0] == "owner-thread"
        assert args[1] == "reviewer.terminal_error"
        summary = args[2]
        assert "mergeability remained unresolved after retries" in summary
        assert "abc123de" in summary
        assert "only if that is still the current head" in summary
        assert "gh run rerun <run-id>" in summary
        assert "gh pr ready 42 --undo" in summary
        assert "gh pr ready 42" in summary
        assert "do not push, add a no-op commit, or add an empty commit" in summary.lower()
        assert "reviewer verdict publishes on that exact head" in summary

    def test_gate_does_not_write_reviewed_sha_when_checks_unreadable(
        self, _reload_gate
    ):
        """An unreadable edge must NOT record a reviewed SHA — the gate must
        stay retryable so the next re-trigger can fire the reviewer."""
        gate_mod = _reload_gate

        with patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=None), \
             patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info"), \
             patch.object(gate_mod, "_run_reviewer"), \
             patch.object(gate_mod, "emit_product_event"), \
             patch.object(github_reviewer_notifications, "notify_checks_unreadable"):

            gate_mod.on_check_suite_completed(
                repo="owner/repo", head_sha="abc123def",
                conclusion="success", pr_number=42,
                internal_event="webhook.github.check_suite_completed",
            )

        assert gate_mod.reviewer_gate_state.get_reviewed_sha("owner/repo#42") is None

    def test_gate_fires_reviewer_after_retrigger_with_readable_checks(
        self, _reload_gate
    ):
        """Recovery path: after checks_unreadable, a second check_suite edge
        with readable checks fires the reviewer normally."""
        gate_mod = _reload_gate
        reviewer_calls = []

        with patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=_DEFAULT_PR_INFO), \
             patch.object(gate_mod, "emit_product_event"), \
             patch.object(github_reviewer_notifications, "notify_checks_unreadable"), \
             patch.object(gate_mod.github_reviewer_gate, "checks_green",
                          side_effect=[None, True]), \
             patch.object(gate_mod, "_run_reviewer",
                          side_effect=lambda *a, **kw: reviewer_calls.append(1)):

            # First edge: checks unreadable → strands with loud event, no review
            gate_mod.on_check_suite_completed(
                repo="owner/repo", head_sha="abc123def",
                conclusion="success", pr_number=42,
                internal_event="webhook.github.check_suite_completed",
            )
            assert reviewer_calls == []

            # Second edge (re-trigger): checks readable + green → reviewer fires
            gate_mod.on_check_suite_completed(
                repo="owner/repo", head_sha="abc123def",
                conclusion="success", pr_number=42,
                internal_event="webhook.github.check_suite_completed",
            )

        assert reviewer_calls == [1]

    def test_notify_owner_checks_unreadable_safe_off_loop(self, _reload_gate):
        """The worker schedules its owner notification on the backend loop."""
        from unittest.mock import AsyncMock

        delivered = AsyncMock(return_value=True)

        with patch("events.delivery.owning_thread_for_identity",
                   return_value="owner-thread"), \
             patch("events.delivery.deliver_to_thread", delivered):
            _call_on_backend_loop(
                github_reviewer_notifications.notify_checks_unreadable,
                "owner/repo", 42, "abc123def",
            )

        delivered.assert_awaited_once()
        args, _ = delivered.call_args
        assert args[0] == "owner-thread"
        assert args[1] == "reviewer.checks_unreadable"
