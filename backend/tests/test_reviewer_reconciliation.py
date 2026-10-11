from __future__ import annotations

import importlib
from unittest.mock import ANY, patch

import pytest
from reviewer_gate_fixtures import _DEFAULT_PR_INFO

REPO = "owner/repo"
HEAD = "abc123def"
PR_NUMBER = 42

# Copied from real cancelled corsia check-run annotations on 2026-10-11.
# Each fixture cites its GitHub Actions workflow run / check run source.
SUPERSESSION = "Canceling since a higher priority waiting request for unit-refs/pull/402/merge exists"
# workflow 38156715956 / check run 114521093057
TIMEOUT = "The job has exceeded the maximum execution time of 25m0s"
# workflow 38089220836 / check run 114322017158
DELIBERATE_CANCEL = "The run was canceled by @corsia-bot."
# workflow 38081362709 / check run 114298778697
GENERIC_CANCEL = "The operation was canceled."
# workflow 38156715956 / check run 114521093057
# No real or documented cancelled-run infrastructure annotation is currently verified.
UNKNOWN_INFRASTRUCTURE = "The runner has received a shutdown signal."


@pytest.fixture
def reconciliation(isolated_db):
    import events.connectors.reviewer_reconciliation as reconciliation_mod

    return importlib.reload(reconciliation_mod)


def _open_prs():
    return [{"number": PR_NUMBER, "head_sha": HEAD}]


def _green_checks():
    return [{"id": 10, "name": "backend", "state": "SUCCESS"}]


def _cancelled_checks():
    return [{"id": 10, "name": "required-ci", "state": "CANCELLED"}]


def _workflow(message: str, *, run_id: int = 99, jobs: list[dict] | None = None):
    return {
        "id": run_id,
        "run_attempt": 1,
        "html_url": f"https://example.test/run/{run_id}",
        "job_name": "required-ci",
        "jobs": jobs or [],
        "annotations": [{"message": message}],
    }


def _reconcile_cancelled(reconciliation, workflow, *, second_workflow=None):
    workflows = [workflow] if second_workflow is None else [workflow, second_workflow]
    return patch.object(reconciliation.github_reviewer_gate, "fetch_required_check_contexts", return_value={"required-ci"}), \
        patch.object(reconciliation.github_reviewer_gate, "fetch_open_pull_requests", return_value=_open_prs()), \
        patch.object(reconciliation.github_reviewer_gate, "fetch_check_runs", return_value=_cancelled_checks()), \
        patch.object(reconciliation.github_reviewer_gate, "fetch_pr_info", return_value=_DEFAULT_PR_INFO), \
        patch.object(reconciliation.github_reviewer_gate, "fetch_workflow_run", side_effect=workflows)


class TestReviewerReconciliation:
    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            (SUPERSESSION, "supersession"),
            (TIMEOUT, "timeout"),
            (DELIBERATE_CANCEL, "deliberate"),
            (GENERIC_CANCEL, "unrecognized"),
        ],
    )
    def test_real_cancelled_annotations_are_never_retryable(self, reconciliation, message, expected):
        """Pin all four sampled corsia cancellation texts to fail-closed outcomes."""
        assert reconciliation._cancel_reason(_workflow(message)) == expected
        assert expected != "infrastructure"

    def test_missed_completion_claims_and_fires_once_across_passes(self, reconciliation):
        with patch.object(reconciliation.github_reviewer_gate, "fetch_required_check_contexts", return_value=set()), \
             patch.object(reconciliation.github_reviewer_gate, "fetch_open_pull_requests", return_value=_open_prs()), \
             patch.object(reconciliation.github_reviewer_gate, "fetch_check_runs", return_value=_green_checks()), \
             patch.object(reconciliation.github_reviewer_gate, "checks_green", return_value=True), \
             patch.object(reconciliation.github_reviewer_gate, "fetch_pr_info_awaiting_mergeability", return_value=_DEFAULT_PR_INFO), \
             patch.object(reconciliation.github_reviewer_gate, "reviewer_status", return_value={}), \
             patch("events.reviewer_gate._evaluate_review_gate") as evaluate:
            reconciliation.reconcile_once([REPO])
            reconciliation.reconcile_once([REPO])

        evaluate.assert_called_once_with(
            REPO, HEAD, PR_NUMBER, reconciliation.RECONCILIATION_EVENT, ANY, checks=_green_checks(),
        )

    def test_stale_pending_reviewer_status_is_recovered_once(self, reconciliation, monkeypatch):
        monkeypatch.setattr(reconciliation.time, "time", lambda: 2_000.0)
        stale_status = {"state": "pending", "created_at": "1970-01-01T00:00:00Z"}

        with patch.object(reconciliation.github_reviewer_gate, "fetch_required_check_contexts", return_value=set()), \
             patch.object(reconciliation.github_reviewer_gate, "fetch_open_pull_requests", return_value=_open_prs()), \
             patch.object(reconciliation.github_reviewer_gate, "fetch_check_runs", return_value=_green_checks()), \
             patch.object(reconciliation.github_reviewer_gate, "checks_green", return_value=True), \
             patch.object(reconciliation.github_reviewer_gate, "fetch_pr_info_awaiting_mergeability", return_value=_DEFAULT_PR_INFO), \
             patch.object(reconciliation.github_reviewer_gate, "reviewer_status", return_value=stale_status), \
             patch("events.reviewer_gate._evaluate_review_gate") as evaluate:
            reconciliation.reconcile_once([REPO])
            reconciliation.reconcile_once([REPO])

        evaluate.assert_called_once_with(
            REPO, HEAD, PR_NUMBER, reconciliation.RECONCILIATION_EVENT, ANY, checks=_green_checks(),
        )

    def test_recent_pending_reviewer_status_is_not_recovered(self, reconciliation, monkeypatch):
        monkeypatch.setattr(reconciliation.time, "time", lambda: 2_000.0)
        recent_status = {"state": "pending", "created_at": "1970-01-01T00:31:00Z"}

        with patch.object(reconciliation.github_reviewer_gate, "fetch_required_check_contexts", return_value=set()), \
             patch.object(reconciliation.github_reviewer_gate, "fetch_open_pull_requests", return_value=_open_prs()), \
             patch.object(reconciliation.github_reviewer_gate, "fetch_check_runs", return_value=_green_checks()), \
             patch.object(reconciliation.github_reviewer_gate, "checks_green", return_value=True), \
             patch.object(reconciliation.github_reviewer_gate, "fetch_pr_info_awaiting_mergeability", return_value=_DEFAULT_PR_INFO), \
             patch.object(reconciliation.github_reviewer_gate, "reviewer_status", return_value=recent_status), \
             patch("events.reviewer_gate._evaluate_review_gate") as evaluate:
            reconciliation.reconcile_once([REPO])

        evaluate.assert_not_called()

    def test_stale_pending_reviewer_status_waits_for_green_checks(self, reconciliation, monkeypatch):
        monkeypatch.setattr(reconciliation.time, "time", lambda: 2_000.0)
        stale_status = {"state": "pending", "created_at": "1970-01-01T00:00:00Z"}

        with patch.object(reconciliation.github_reviewer_gate, "fetch_required_check_contexts", return_value=set()), \
             patch.object(reconciliation.github_reviewer_gate, "fetch_open_pull_requests", return_value=_open_prs()), \
             patch.object(reconciliation.github_reviewer_gate, "fetch_check_runs", return_value=_green_checks()), \
             patch.object(reconciliation.github_reviewer_gate, "checks_green", return_value=False), \
             patch.object(reconciliation.github_reviewer_gate, "fetch_pr_info_awaiting_mergeability", return_value=_DEFAULT_PR_INFO), \
             patch.object(reconciliation.github_reviewer_gate, "reviewer_status", return_value=stale_status), \
             patch("events.reviewer_gate._evaluate_review_gate") as evaluate:
            reconciliation.reconcile_once([REPO])

        evaluate.assert_not_called()

    def test_stale_pending_reviewer_status_respects_existing_in_flight_claim(self, reconciliation, monkeypatch):
        monkeypatch.setattr(reconciliation.time, "time", lambda: 2_000.0)
        stale_status = {"state": "pending", "created_at": "1970-01-01T00:00:00Z"}
        pr_key = reconciliation.reviewer_gate_state.pr_key(REPO, PR_NUMBER)
        assert reconciliation.reviewer_gate_state.claim_in_flight(pr_key, HEAD)
        try:
            with patch.object(reconciliation.github_reviewer_gate, "fetch_required_check_contexts", return_value=set()), \
                 patch.object(reconciliation.github_reviewer_gate, "fetch_open_pull_requests", return_value=_open_prs()), \
                 patch.object(reconciliation.github_reviewer_gate, "fetch_check_runs", return_value=_green_checks()), \
                 patch.object(reconciliation.github_reviewer_gate, "checks_green", return_value=True), \
                 patch.object(reconciliation.github_reviewer_gate, "fetch_pr_info_awaiting_mergeability", return_value=_DEFAULT_PR_INFO), \
                 patch.object(reconciliation.github_reviewer_gate, "reviewer_status", return_value=stale_status), \
                 patch("events.reviewer_gate._evaluate_review_gate") as evaluate:
                reconciliation.reconcile_once([REPO])
        finally:
            reconciliation.reviewer_gate_state.release_in_flight(pr_key, HEAD)

        evaluate.assert_not_called()

    def test_missed_completion_waits_for_mergeability_before_claiming(self, reconciliation):
        not_mergeable = {**_DEFAULT_PR_INFO, "mergeable": "CONFLICTING"}
        with patch.object(reconciliation.github_reviewer_gate, "fetch_required_check_contexts", return_value=set()), \
             patch.object(reconciliation.github_reviewer_gate, "fetch_open_pull_requests", return_value=_open_prs()), \
             patch.object(reconciliation.github_reviewer_gate, "fetch_check_runs", return_value=_green_checks()), \
             patch.object(reconciliation.github_reviewer_gate, "checks_green", return_value=True), \
             patch.object(reconciliation.github_reviewer_gate, "reviewer_status", return_value={}), \
             patch.object(reconciliation.github_reviewer_gate, "fetch_pr_info_awaiting_mergeability", side_effect=[not_mergeable, _DEFAULT_PR_INFO]), \
             patch("events.reviewer_gate._evaluate_review_gate") as evaluate:
            reconciliation.reconcile_once([REPO])
            reconciliation.reconcile_once([REPO])

        evaluate.assert_called_once_with(
            REPO, HEAD, PR_NUMBER, reconciliation.RECONCILIATION_EVENT, ANY, checks=_green_checks(),
        )

    def test_supersession_annotation_is_ignored(self, reconciliation):
        patches = _reconcile_cancelled(reconciliation, _workflow(SUPERSESSION))
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patch.object(reconciliation.github_reviewer_gate, "rerun_failed_jobs") as rerun, \
             patch.object(reconciliation.github_reviewer_notifications, "notify_required_check_cancelled") as notify:
            reconciliation.reconcile_once([REPO])

        rerun.assert_not_called()
        notify.assert_not_called()

    def test_timeout_annotation_routes_owner_without_rerun(self, reconciliation):
        workflow = _workflow(TIMEOUT)
        patches = _reconcile_cancelled(reconciliation, workflow)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patch.object(reconciliation.github_reviewer_gate, "rerun_failed_jobs") as rerun, \
             patch.object(reconciliation.github_reviewer_notifications, "notify_required_check_timeout") as timeout:
            reconciliation.reconcile_once([REPO])

        timeout.assert_called_once_with(REPO, PR_NUMBER, HEAD, workflow, ANY)
        rerun.assert_not_called()

    def test_deliberate_cancel_annotation_is_ignored(self, reconciliation):
        patches = _reconcile_cancelled(reconciliation, _workflow(DELIBERATE_CANCEL))
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patch.object(reconciliation.github_reviewer_gate, "rerun_failed_jobs") as rerun, \
             patch.object(reconciliation.github_reviewer_notifications, "notify_required_check_cancelled") as notify:
            reconciliation.reconcile_once([REPO])

        rerun.assert_not_called()
        notify.assert_not_called()

    def test_unknown_cancel_routes_owner_without_rerun(self, reconciliation):
        workflow = _workflow(GENERIC_CANCEL)
        patches = _reconcile_cancelled(reconciliation, workflow)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patch.object(reconciliation.github_reviewer_gate, "rerun_failed_jobs") as rerun, \
             patch.object(reconciliation.github_reviewer_notifications, "notify_required_check_cancelled") as notify:
            reconciliation.reconcile_once([REPO])

        rerun.assert_not_called()
        notify.assert_called_once_with(REPO, PR_NUMBER, HEAD, workflow, "unrecognized", ANY)

    def test_failed_sibling_routes_owner_without_rerun(self, reconciliation):
        workflow = _workflow(UNKNOWN_INFRASTRUCTURE, jobs=[{"conclusion": "failure"}])
        patches = _reconcile_cancelled(reconciliation, workflow)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patch.object(reconciliation.github_reviewer_gate, "rerun_failed_jobs") as rerun, \
             patch.object(reconciliation.github_reviewer_notifications, "notify_required_check_cancelled") as notify:
            reconciliation.reconcile_once([REPO])

        rerun.assert_not_called()
        notify.assert_called_once_with(REPO, PR_NUMBER, HEAD, workflow, "failed_sibling", ANY)

    def test_unverified_infrastructure_text_routes_owner_without_rerun(self, reconciliation):
        workflow = _workflow(UNKNOWN_INFRASTRUCTURE)
        patches = _reconcile_cancelled(reconciliation, workflow)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
             patch.object(reconciliation.github_reviewer_gate, "rerun_failed_jobs") as rerun, \
             patch.object(reconciliation.github_reviewer_notifications, "notify_required_check_cancelled") as notify:
            reconciliation.reconcile_once([REPO])

        rerun.assert_not_called()
        notify.assert_called_once_with(REPO, PR_NUMBER, HEAD, workflow, "unrecognized", ANY)
