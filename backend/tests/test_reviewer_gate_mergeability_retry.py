from __future__ import annotations

from typing import Callable
from unittest.mock import Mock, patch

from reviewer_gate_fixtures import _DEFAULT_PR_INFO, _reload_gate, _stub_presence  # noqa: F401


class _RetryLoop:
    def __init__(self) -> None:
        self.scheduled: list[tuple[float, object, tuple[object, ...]]] = []

    def is_closed(self) -> bool:
        return False

    def call_soon_threadsafe(self, callback, *args) -> None:
        callback(*args)

    def call_later(self, delay: float, callback, *args) -> None:
        self.scheduled.append((delay, callback, args))


class _RetryTask:
    def __init__(self) -> None:
        self.callbacks: list[Callable[[_RetryTask], None]] = []

    def add_done_callback(self, callback: Callable[[_RetryTask], None]) -> None:
        self.callbacks.append(callback)

    def result(self) -> None:
        return None

    def finish(self) -> None:
        for callback in self.callbacks:
            callback(self)


class TestMergeabilityRetry:
    def test_pr_info_fetch_retries_then_gate_proceeds(self, _reload_gate):
        gate_mod = _reload_gate
        ready = {**_DEFAULT_PR_INFO, "mergeable": "MERGEABLE"}

        with patch.object(
            gate_mod.github_reviewer_gate,
            "fetch_pr_info",
            side_effect=[None, ready],
        ) as fetch_pr_info, patch.object(
            gate_mod.github_reviewer_gate.time,
            "sleep",
        ) as sleep, patch.object(
            gate_mod.github_reviewer_gate,
            "checks_green",
            return_value=True,
        ), patch.object(gate_mod, "_run_reviewer") as run_reviewer, patch.object(
            gate_mod,
            "emit_product_event",
        ), patch.object(
            gate_mod.github_reviewer_notifications,
            "notify_pr_info_unreadable",
        ), patch("reviewer.reviewer_status.set_error"):
            gate_mod._evaluate_review_gate(
                "owner/repo", "abc123def", 42, "webhook.github.check_suite_completed", None,
            )

        assert fetch_pr_info.call_count == 2
        sleep.assert_called_once_with(gate_mod.github_reviewer_gate.MERGEABILITY_POLL_INTERVAL_S)
        run_reviewer.assert_called_once()

    def test_pr_info_fetch_exhaustion_returns_none_after_full_budget(self, _reload_gate):
        gate_mod = _reload_gate

        with patch.object(
            gate_mod.github_reviewer_gate,
            "fetch_pr_info",
            return_value=None,
        ) as fetch_pr_info, patch.object(
            gate_mod.github_reviewer_gate.time,
            "sleep",
        ) as sleep:
            result = gate_mod.github_reviewer_gate.fetch_pr_info_awaiting_mergeability(
                "owner/repo", 42, "abc123def",
            )

        assert result is None
        assert fetch_pr_info.call_count == gate_mod.github_reviewer_gate.MERGEABILITY_POLL_ATTEMPTS
        assert sleep.call_count == gate_mod.github_reviewer_gate.MERGEABILITY_POLL_ATTEMPTS - 1

    def test_unknown_then_mergeable_retry_fires_reviewer(self, _reload_gate):
        gate_mod = _reload_gate
        unknown = {**_DEFAULT_PR_INFO, "mergeable": "UNKNOWN"}
        ready = {**_DEFAULT_PR_INFO, "mergeable": "MERGEABLE"}
        fired = []

        with patch.object(
            gate_mod.github_reviewer_gate,
            "fetch_pr_info_awaiting_mergeability",
            side_effect=[unknown, ready],
        ), patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), patch.object(
            gate_mod, "_schedule_mergeability_retry", return_value=True,
        ) as schedule, patch.object(
            gate_mod, "_run_reviewer", side_effect=lambda *_args: fired.append(True),
        ), patch.object(gate_mod, "emit_product_event"):
            gate_mod._evaluate_review_gate(
                "owner/repo", "abc123def", 42, "webhook.github.check_suite_completed", None,
            )
            gate_mod._evaluate_review_gate(
                "owner/repo", "abc123def", 42, "webhook.github.check_suite_completed", None,
                mergeability_retry_attempt=1,
            )

        schedule.assert_called_once()
        assert fired == [True]

    def test_persistent_unknown_reports_error_and_allows_current_head_refire(self, _reload_gate):
        gate_mod = _reload_gate
        unknown = {**_DEFAULT_PR_INFO, "mergeable": "UNKNOWN"}
        emitted = []

        with patch.object(
            gate_mod.github_reviewer_gate,
            "fetch_pr_info_awaiting_mergeability",
            side_effect=[unknown, _DEFAULT_PR_INFO],
        ), patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), patch.object(
            gate_mod,
            "emit_product_event",
            side_effect=lambda scope, event, **_kw: emitted.append((scope, event)),
        ), patch.object(
            gate_mod.github_reviewer_notifications, "notify_reviewer_terminal_error",
        ) as notify_error, patch(
            "reviewer.reviewer_status.set_error",
        ) as set_error, patch.object(gate_mod, "_run_reviewer") as run_reviewer:
            gate_mod._evaluate_review_gate(
                "owner/repo", "abc123def", 42, "webhook.github.check_suite_completed", None,
                mergeability_retry_attempt=len(gate_mod.MERGEABILITY_RETRY_DELAYS_S),
            )
            gate_mod._evaluate_review_gate(
                "owner/repo", "abc123def", 42, "webhook.github.check_suite_completed", None,
            )

        set_error.assert_called_once_with(
            "owner/repo",
            "abc123def",
            "qa/llm-reviewer: mergeability remained unresolved after retries; retry required",
        )
        notify_error.assert_called_once_with(
            "owner/repo", 42, "abc123def", "mergeability remained unresolved after retries", None,
        )
        assert ("reviewer_gate", "mergeability_unresolved") in emitted
        run_reviewer.assert_called_once()

    def test_conflicting_holds_without_scheduling_retry(self, _reload_gate):
        gate_mod = _reload_gate
        conflicting = {**_DEFAULT_PR_INFO, "mergeable": "CONFLICTING"}

        with patch.object(
            gate_mod.github_reviewer_gate,
            "fetch_pr_info_awaiting_mergeability",
            return_value=conflicting,
        ), patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), patch.object(
            gate_mod, "_schedule_mergeability_retry",
        ) as schedule, patch.object(gate_mod, "_run_reviewer") as run_reviewer, patch.object(
            gate_mod, "emit_product_event",
        ):
            gate_mod._evaluate_review_gate(
                "owner/repo", "abc123def", 42, "webhook.github.check_suite_completed", None,
            )

        schedule.assert_not_called()
        run_reviewer.assert_not_called()

    def test_head_moved_before_retry_does_not_review(self, _reload_gate):
        gate_mod = _reload_gate
        moved = {**_DEFAULT_PR_INFO, "headRefOid": "new-head", "mergeable": "MERGEABLE"}

        with patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), patch.object(
            gate_mod.github_reviewer_gate, "fetch_pr_info_awaiting_mergeability", return_value=moved,
        ), patch.object(gate_mod, "_run_reviewer") as run_reviewer, patch.object(
            gate_mod, "emit_product_event",
        ):
            gate_mod._evaluate_review_gate(
                "owner/repo", "abc123def", 42, "webhook.github.check_suite_completed", None,
                mergeability_retry_attempt=1,
            )

        run_reviewer.assert_not_called()

    def test_already_reviewed_head_is_noop_on_retry(self, _reload_gate):
        gate_mod = _reload_gate
        gate_mod.reviewer_gate_state.mark_reviewed("owner/repo#42", "abc123def")

        with patch.object(gate_mod.github_reviewer_gate, "checks_green") as checks_green, patch.object(
            gate_mod.github_reviewer_gate, "fetch_pr_info_awaiting_mergeability",
        ) as fetch_pr_info, patch.object(gate_mod, "_run_reviewer") as run_reviewer, patch.object(
            gate_mod, "emit_product_event",
        ):
            gate_mod._evaluate_review_gate(
                "owner/repo", "abc123def", 42, "webhook.github.check_suite_completed", None,
                mergeability_retry_attempt=1,
            )

        checks_green.assert_not_called()
        fetch_pr_info.assert_not_called()
        run_reviewer.assert_not_called()

    def test_retry_uses_the_supplied_event_loop_to_reenter_normal_evaluation(self, _reload_gate):
        gate_mod = _reload_gate
        loop = _RetryLoop()

        assert gate_mod._schedule_mergeability_retry(
            loop, "owner/repo", "abc123def", 42, "webhook.github.check_suite_completed", 0,
        ) is True
        assert len(loop.scheduled) == 1
        delay, callback, args = loop.scheduled[0]
        assert delay == gate_mod.MERGEABILITY_RETRY_DELAYS_S[0]

        task = _RetryTask()
        to_thread = Mock()
        with patch.object(gate_mod.asyncio, "create_task", return_value=task), patch.object(
            gate_mod.asyncio, "to_thread", new=to_thread,
        ):
            callback(*args)

        to_thread.assert_called_once_with(
            gate_mod._evaluate_review_gate,
            "owner/repo",
            "abc123def",
            42,
            "webhook.github.check_suite_completed",
            loop,
            1,
        )
        assert task in gate_mod._PENDING_RETRIES
        task.finish()
        assert task not in gate_mod._PENDING_RETRIES


class TestChecksGreenReadRetry:
    """The check-run read re-polls an unreadable result with the same bounded
    budget as the PR-info read (#1168), so one brief GitHub error cannot make
    the gate emit checks_unreadable and wake the owner."""

    def test_unreadable_read_retries_then_reports_green(self, _reload_gate):
        gate_mod = _reload_gate
        green = [{"name": "Backend unit tests", "state": "SUCCESS"}]

        with patch(
            "reviewer.gh_helpers.get_check_runs_for_sha",
            side_effect=[None, green],
        ) as fetch_checks, patch.object(
            gate_mod.github_reviewer_gate.time, "sleep",
        ) as sleep:
            result = gate_mod.github_reviewer_gate.checks_green(
                "owner/repo", 42, "abc123def",
            )

        assert result is True
        assert fetch_checks.call_count == 2
        sleep.assert_called_once_with(
            gate_mod.github_reviewer_gate.MERGEABILITY_POLL_INTERVAL_S,
        )

    def test_persistent_unreadable_exhausts_full_budget_then_returns_none(
        self, _reload_gate
    ):
        gate_mod = _reload_gate

        with patch(
            "reviewer.gh_helpers.get_check_runs_for_sha", return_value=None,
        ) as fetch_checks, patch.object(
            gate_mod.github_reviewer_gate.time, "sleep",
        ) as sleep:
            result = gate_mod.github_reviewer_gate.checks_green(
                "owner/repo", 42, "abc123def",
            )

        assert result is None
        assert fetch_checks.call_count == (
            gate_mod.github_reviewer_gate.MERGEABILITY_POLL_ATTEMPTS
        )
        assert sleep.call_count == (
            gate_mod.github_reviewer_gate.MERGEABILITY_POLL_ATTEMPTS - 1
        )

    def test_readable_pending_result_returns_without_retrying(self, _reload_gate):
        gate_mod = _reload_gate
        pending = [
            {"name": "Backend unit tests", "state": "SUCCESS"},
            {"name": "Frontend unit tests", "state": "PENDING"},
        ]

        with patch(
            "reviewer.gh_helpers.get_check_runs_for_sha", return_value=pending,
        ) as fetch_checks, patch.object(
            gate_mod.github_reviewer_gate.time, "sleep",
        ) as sleep:
            result = gate_mod.github_reviewer_gate.checks_green(
                "owner/repo", 42, "abc123def",
            )

        assert result is False
        assert fetch_checks.call_count == 1
        sleep.assert_not_called()
