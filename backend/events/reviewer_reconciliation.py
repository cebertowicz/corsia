"""Periodic reconciliation pass that reuses the canonical reviewer gate."""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger as log

from events import reviewer_gate_state
from events.connectors import github_reviewer_gate, github_reviewer_notifications

RECONCILIATION_INTERVAL_S = 5 * 60
RECONCILIATION_EVENT = "automation.github.reviewer_sweep"


def reconcile_once(
    repositories: list[str] | tuple[str, ...],
    loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    """Reconcile missed reviewer completions and cancelled required CI once."""
    for repo in repositories:
        required_contexts = github_reviewer_gate.fetch_required_check_contexts(repo, "dev")
        if required_contexts is None:
            log.error("reviewer_sweep: skip repo={} reason=required_contexts_unreadable", repo)
            continue
        pull_requests = github_reviewer_gate.fetch_open_pull_requests(repo, "dev")
        if pull_requests is None:
            log.error("reviewer_sweep: skip repo={} reason=open_prs_unreadable", repo)
            continue
        for pull_request in pull_requests:
            _reconcile_pull_request(repo, pull_request, required_contexts, loop)


def _reconcile_pull_request(
    repo: str,
    pull_request: dict[str, Any],
    required_contexts: set[str],
    loop: asyncio.AbstractEventLoop | None,
) -> None:
    """Run the canonical gate or repair one readable cancelled required check."""
    pr_number = pull_request.get("number")
    head_sha = pull_request.get("head_sha")
    if not isinstance(pr_number, int) or not isinstance(head_sha, str) or not head_sha:
        log.error("reviewer_sweep: skip repo={} reason=malformed_open_pr", repo)
        return
    pr_key = reviewer_gate_state.pr_key(repo, pr_number)
    if reviewer_gate_state.get_reviewed_sha(pr_key) == head_sha:
        log.info("reviewer_sweep: skip pr={} head={} reason=already_reviewed", pr_number, head_sha[:8])
        return
    checks = github_reviewer_gate.fetch_check_runs(repo, head_sha)
    if checks is None:
        log.error("reviewer_sweep: skip pr={} head={} reason=check_runs_unreadable", pr_number, head_sha[:8])
        return
    cancelled = [
        check for check in checks
        if check.get("name") in required_contexts
        and str(check.get("state") or check.get("conclusion") or "").upper() == "CANCELLED"
    ]
    if cancelled:
        _reconcile_cancelled_required_checks(repo, pr_number, head_sha, pr_key, cancelled, loop)
        return
    status = github_reviewer_gate.reviewer_status(repo, head_sha)
    if status is None:
        log.error("reviewer_sweep: skip pr={} head={} reason=reviewer_status_unreadable", pr_number, head_sha[:8])
        return
    if status:
        log.info(
            "reviewer_sweep: skip pr={} head={} reason=reviewer_status_{}",
            pr_number, head_sha[:8], str(status.get("state") or "present").lower(),
        )
        return
    if not github_reviewer_gate.checks_green(repo, pr_number, head_sha, checks):
        log.info("reviewer_sweep: skip pr={} head={} reason=checks_not_green", pr_number, head_sha[:8])
        return
    from events.reviewer_gate import _evaluate_review_gate

    log.info("reviewer_sweep: fired pr={} head={} reason=missed_completion", pr_number, head_sha[:8])
    _evaluate_review_gate(repo, head_sha, pr_number, RECONCILIATION_EVENT, loop, checks=checks)


def _reconcile_cancelled_required_checks(
    repo: str,
    pr_number: int,
    head_sha: str,
    pr_key: str,
    checks: list[dict[str, Any]],
    loop: asyncio.AbstractEventLoop | None,
) -> None:
    """Classify each cancelled required context before making any retry decision."""
    pr_info = github_reviewer_gate.fetch_pr_info(repo, pr_number)
    if pr_info is None:
        log.error("reviewer_sweep: skip pr={} head={} reason=pr_info_unreadable", pr_number, head_sha[:8])
        return
    if pr_info.get("headRefOid") != head_sha:
        log.info("reviewer_sweep: skip pr={} head={} reason=superseded", pr_number, head_sha[:8])
        return
    for check in checks:
        workflow_run = github_reviewer_gate.fetch_workflow_run(repo, check)
        if workflow_run is None:
            log.error(
                "reviewer_sweep: skip pr={} head={} reason=workflow_run_unreadable check={}",
                pr_number, head_sha[:8], check.get("name"),
            )
            continue
        reason = _cancel_reason(check, workflow_run)
        if reason == "supersession":
            log.info("reviewer_sweep: skip pr={} head={} reason=supersession", pr_number, head_sha[:8])
            continue
        if reason == "timeout":
            github_reviewer_notifications.notify_required_check_timeout(
                repo, pr_number, head_sha, workflow_run, loop,
            )
            log.warning("reviewer_sweep: routed pr={} head={} reason=timeout", pr_number, head_sha[:8])
            continue
        run_id = workflow_run.get("id")
        attempt = workflow_run.get("run_attempt", 1)
        if not isinstance(run_id, int) or not isinstance(attempt, int):
            log.error("reviewer_sweep: skip pr={} head={} reason=workflow_run_malformed", pr_number, head_sha[:8])
            continue
        if not reviewer_gate_state.claim_workflow_rerun(pr_key, run_id, attempt):
            log.info("reviewer_sweep: skip pr={} head={} reason=rerun_already_claimed", pr_number, head_sha[:8])
            continue
        if github_reviewer_gate.rerun_workflow_run(repo, run_id):
            log.info("reviewer_sweep: rerun pr={} head={} reason=infrastructure", pr_number, head_sha[:8])
            continue
        reviewer_gate_state.release_workflow_rerun(pr_key, run_id, attempt)
        log.error("reviewer_sweep: skip pr={} head={} reason=rerun_request_failed", pr_number, head_sha[:8])


def _cancel_reason(check: dict[str, Any], workflow_run: dict[str, Any]) -> str:
    """Classify cancellation from GitHub's run, job and annotation detail."""
    detail_values: list[Any] = [
        check.get("conclusion_detail"),
        check.get("details"),
        (check.get("output") or {}).get("title") if isinstance(check.get("output"), dict) else "",
        (check.get("output") or {}).get("summary") if isinstance(check.get("output"), dict) else "",
        workflow_run.get("conclusion_detail"),
        workflow_run.get("name"),
    ]
    for job in workflow_run.get("jobs") or []:
        if isinstance(job, dict):
            detail_values.extend((job.get("name"), job.get("conclusion"), job.get("status")))
            for step in job.get("steps") or []:
                if isinstance(step, dict):
                    detail_values.extend((step.get("name"), step.get("conclusion"), step.get("status")))
    for annotation in workflow_run.get("annotations") or []:
        if isinstance(annotation, dict):
            detail_values.extend((annotation.get("title"), annotation.get("message"), annotation.get("raw_details")))
    detail = " ".join(str(value or "") for value in detail_values).lower().replace("_", " ").replace("-", " ")
    if "timeout" in detail or "timed out" in detail or "time limit" in detail:
        return "timeout"
    if "supersed" in detail or "newer run" in detail or "concurrency" in detail:
        return "supersession"
    return "infrastructure"


async def loop() -> None:
    """Run the bounded reconciliation cadence under the backend runtime owner."""
    from events.connectors.github_notifications_feed import tracked_github_repositories

    while True:
        try:
            repositories = await asyncio.to_thread(tracked_github_repositories)
            await asyncio.to_thread(reconcile_once, tuple(repositories), asyncio.get_running_loop())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("reviewer_sweep: pass failed")
        await asyncio.sleep(RECONCILIATION_INTERVAL_S)
