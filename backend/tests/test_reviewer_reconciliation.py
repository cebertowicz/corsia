from __future__ import annotations

import importlib
from unittest.mock import ANY, patch

import pytest
from reviewer_gate_fixtures import _DEFAULT_PR_INFO


@pytest.fixture
def _reload_gate(isolated_db):
    import events.reviewer_gate as gate_mod
    import events.reviewer_reconciliation as reconciliation_mod

    gate_mod = importlib.reload(gate_mod)
    reconciliation_mod = importlib.reload(reconciliation_mod)
    gate_mod.reconcile_reviewer_gate_once = reconciliation_mod.reconcile_once
    gate_mod.RECONCILIATION_EVENT = reconciliation_mod.RECONCILIATION_EVENT
    gate_mod.reconciliation_log = reconciliation_mod.log
    return gate_mod


REPO = "owner/repo"
HEAD = "abc123def"
PR_NUMBER = 42


def _open_prs():
    return [{"number": PR_NUMBER, "head_sha": HEAD}]


def _green_checks():
    return [{"id": 10, "name": "backend", "state": "SUCCESS"}]


class TestReviewerReconciliation:
    def test_missed_completion_fires_once(self, _reload_gate):
        gate_mod = _reload_gate

        with patch.object(gate_mod.github_reviewer_gate, "fetch_required_check_contexts", return_value=set()), \
             patch.object(gate_mod.github_reviewer_gate, "fetch_open_pull_requests", return_value=_open_prs()), \
             patch.object(gate_mod.github_reviewer_gate, "fetch_check_runs", return_value=_green_checks()), \
             patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch.object(gate_mod.github_reviewer_gate, "reviewer_status", return_value={}), \
             patch.object(gate_mod, "_evaluate_review_gate") as evaluate:

            gate_mod.reconcile_reviewer_gate_once([REPO])

        evaluate.assert_called_once_with(
            REPO, HEAD, PR_NUMBER, gate_mod.RECONCILIATION_EVENT, ANY, checks=_green_checks(),
        )

    def test_second_pass_does_not_refire_a_reviewed_head(self, _reload_gate):
        gate_mod = _reload_gate
        gate_mod.reviewer_gate_state.mark_reviewed(f"{REPO}#{PR_NUMBER}", HEAD)

        with patch.object(gate_mod.github_reviewer_gate, "fetch_required_check_contexts", return_value=set()), \
             patch.object(gate_mod.github_reviewer_gate, "fetch_open_pull_requests", return_value=_open_prs()), \
             patch.object(gate_mod, "_evaluate_review_gate") as evaluate:

            gate_mod.reconcile_reviewer_gate_once([REPO])

        evaluate.assert_not_called()

    def test_pending_reviewer_status_does_not_duplicate_a_run(self, _reload_gate):
        gate_mod = _reload_gate

        with patch.object(gate_mod.github_reviewer_gate, "fetch_required_check_contexts", return_value=set()), \
             patch.object(gate_mod.github_reviewer_gate, "fetch_open_pull_requests", return_value=_open_prs()), \
             patch.object(gate_mod.github_reviewer_gate, "fetch_check_runs", return_value=_green_checks()), \
             patch.object(gate_mod.github_reviewer_gate, "checks_green", return_value=True), \
             patch.object(gate_mod.github_reviewer_gate, "reviewer_status", return_value={"state": "pending"}), \
             patch.object(gate_mod, "_evaluate_review_gate") as evaluate:

            gate_mod.reconcile_reviewer_gate_once([REPO])

        evaluate.assert_not_called()

    def test_superseded_cancelled_required_check_is_ignored(self, _reload_gate):
        gate_mod = _reload_gate
        cancelled = [{"id": 10, "name": "required-ci", "state": "CANCELLED"}]
        stale_pr = {**_DEFAULT_PR_INFO, "headRefOid": "newer-head"}

        with patch.object(gate_mod.github_reviewer_gate, "fetch_required_check_contexts", return_value={"required-ci"}), \
             patch.object(gate_mod.github_reviewer_gate, "fetch_open_pull_requests", return_value=_open_prs()), \
             patch.object(gate_mod.github_reviewer_gate, "fetch_check_runs", return_value=cancelled), \
             patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=stale_pr), \
             patch.object(gate_mod.github_reviewer_gate, "fetch_workflow_run") as workflow, \
             patch.object(gate_mod.github_reviewer_gate, "rerun_workflow_run") as rerun:

            gate_mod.reconcile_reviewer_gate_once([REPO])

        workflow.assert_not_called()
        rerun.assert_not_called()

    def test_timeout_is_routed_to_owner_not_rerun(self, _reload_gate):
        gate_mod = _reload_gate
        cancelled = [{"id": 10, "name": "required-ci", "state": "CANCELLED"}]
        timed_out = {
            "id": 99,
            "run_attempt": 1,
            "html_url": "https://example.test/run/99",
            "jobs": [{"name": "slow-required-job", "steps": [{"name": "run", "conclusion": "timed_out"}]}],
        }

        with patch.object(gate_mod.github_reviewer_gate, "fetch_required_check_contexts", return_value={"required-ci"}), \
             patch.object(gate_mod.github_reviewer_gate, "fetch_open_pull_requests", return_value=_open_prs()), \
             patch.object(gate_mod.github_reviewer_gate, "fetch_check_runs", return_value=cancelled), \
             patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=_DEFAULT_PR_INFO), \
             patch.object(gate_mod.github_reviewer_gate, "fetch_workflow_run", return_value=timed_out), \
             patch.object(gate_mod.github_reviewer_notifications, "notify_required_check_timeout") as notify, \
             patch.object(gate_mod.github_reviewer_gate, "rerun_workflow_run") as rerun:

            gate_mod.reconcile_reviewer_gate_once([REPO])

        notify.assert_called_once_with(REPO, PR_NUMBER, HEAD, timed_out, ANY)
        rerun.assert_not_called()

    def test_infrastructure_cancel_is_rerun_exactly_once(self, _reload_gate):
        gate_mod = _reload_gate
        cancelled = [{"id": 10, "name": "required-ci", "state": "CANCELLED"}]
        infrastructure = {"id": 99, "run_attempt": 1, "html_url": "https://example.test/run/99"}

        with patch.object(gate_mod.github_reviewer_gate, "fetch_required_check_contexts", return_value={"required-ci"}), \
             patch.object(gate_mod.github_reviewer_gate, "fetch_open_pull_requests", return_value=_open_prs()), \
             patch.object(gate_mod.github_reviewer_gate, "fetch_check_runs", return_value=cancelled), \
             patch.object(gate_mod.github_reviewer_gate, "fetch_pr_info", return_value=_DEFAULT_PR_INFO), \
             patch.object(gate_mod.github_reviewer_gate, "fetch_workflow_run", return_value=infrastructure), \
             patch.object(gate_mod.github_reviewer_gate, "rerun_workflow_run", return_value=True) as rerun:

            gate_mod.reconcile_reviewer_gate_once([REPO])
            gate_mod.reconcile_reviewer_gate_once([REPO])

        rerun.assert_called_once_with(REPO, 99)

    def test_github_read_failure_is_logged_not_swallowed(self, _reload_gate):
        gate_mod = _reload_gate

        with patch.object(gate_mod.github_reviewer_gate, "fetch_required_check_contexts", return_value=None), \
             patch.object(gate_mod.reconciliation_log, "error") as error:

            gate_mod.reconcile_reviewer_gate_once([REPO])

        assert any("reviewer_sweep:" in str(call.args[0]) and "required_contexts_unreadable" in str(call.args[0])
                   for call in error.call_args_list)
