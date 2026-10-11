"""Periodic reconciliation pass that reuses the canonical reviewer gate."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

from loguru import logger as log

from events import reviewer_gate_state
from events.connectors import github_reviewer_gate, github_reviewer_notifications
from reviewer.reviewer_backends import REVIEWER_RUN_TIMEOUT_S

RECONCILIATION_INTERVAL_S = 5 * 60
RECONCILIATION_EVENT = "automation.github.reviewer_sweep"

# Real corsia annotation prefixes, sampled 2026-10-11:
# check run 114521093057 (workflow 38156715956), check run 114322017158
# (workflow 38089220836), and check run 114298778697 (workflow 38081362709).
_SUPERSESSION_PREFIX = "Canceling since a higher priority waiting request for "
_TIMEOUT_PREFIX = "The job has exceeded the maximum execution time"
_DELIBERATE_CANCEL_PREFIX = "The run was canceled by @"
# No real or documented cancelled-run infrastructure annotation has been
# verified. Adding an entry is the one-line, evidence-backed data change that
# enables the existing narrow rerun path; each entry needs a real API fixture.
_INFRASTRUCTURE_ANNOTATION_PREFIXES: tuple[str, ...] = ()


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
    """Run the canonical gate or fail closed on a cancelled required check."""
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
        if _stuck_pending_status(status):
            if not github_reviewer_gate.checks_green(repo, pr_number, head_sha, checks):
                log.info("reviewer_sweep: skip pr={} head={} reason=checks_not_green", pr_number, head_sha[:8])
                return
            _fire_reconciliation_review(repo, pr_number, head_sha, pr_key, checks, loop, "pending_recovery")
            return
        log.info(
            "reviewer_sweep: skip pr={} head={} reason=reviewer_status_{}",
            pr_number, head_sha[:8], str(status.get("state") or "present").lower(),
        )
        return
    if not github_reviewer_gate.checks_green(repo, pr_number, head_sha, checks):
        log.info("reviewer_sweep: skip pr={} head={} reason=checks_not_green", pr_number, head_sha[:8])
        return
    _fire_reconciliation_review(repo, pr_number, head_sha, pr_key, checks, loop, "missed_completion")


def _fire_reconciliation_review(
    repo: str,
    pr_number: int,
    head_sha: str,
    pr_key: str,
    checks: list[dict[str, Any]],
    loop: asyncio.AbstractEventLoop | None,
    action: str,
) -> None:
    """Claim and send exactly one reconciliation recovery into the canonical gate."""
    pr_info = github_reviewer_gate.fetch_pr_info_awaiting_mergeability(repo, pr_number, head_sha)
    if pr_info is None:
        log.error("reviewer_sweep: skip pr={} head={} reason=pr_info_unreadable", pr_number, head_sha[:8])
        return
    if pr_info.get("headRefOid") != head_sha:
        log.info("reviewer_sweep: skip pr={} head={} reason=superseded", pr_number, head_sha[:8])
        return
    if github_reviewer_gate.hold_pr_until_reviewable(repo, pr_number, head_sha, pr_info):
        log.info("reviewer_sweep: skip pr={} head={} reason=not_mergeable", pr_number, head_sha[:8])
        return
    if reviewer_gate_state.is_in_flight(pr_key, head_sha):
        log.info("reviewer_sweep: skip pr={} head={} reason=review_in_flight", pr_number, head_sha[:8])
        return
    if not reviewer_gate_state.claim_reconciliation_action(pr_key, head_sha, action):
        log.info("reviewer_sweep: skip pr={} head={} reason=review_already_claimed", pr_number, head_sha[:8])
        return
    from events.reviewer_gate import _evaluate_review_gate

    log.info("reviewer_sweep: fired pr={} head={} reason={}", pr_number, head_sha[:8], action)
    _evaluate_review_gate(repo, head_sha, pr_number, RECONCILIATION_EVENT, loop, checks=checks)


def _stuck_pending_status(status: dict[str, str]) -> bool:
    """Return whether a timestamped reviewer pending status exceeded its run limit."""
    if str(status.get("state") or "").lower() != "pending":
        return False
    created_at = status.get("created_at") or status.get("updated_at")
    if not isinstance(created_at, str):
        return False
    try:
        started = datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return False
    return time.time() - started >= REVIEWER_RUN_TIMEOUT_S


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
        if _has_failed_job(workflow_run):
            _route_cancelled_check(repo, pr_number, head_sha, workflow_run, "failed_sibling", loop)
            continue
        reason = _cancel_reason(workflow_run)
        if reason in {"supersession", "deliberate"}:
            log.info("reviewer_sweep: skip pr={} head={} reason={}", pr_number, head_sha[:8], reason)
            continue
        if reason == "timeout":
            github_reviewer_notifications.notify_required_check_timeout(
                repo, pr_number, head_sha, workflow_run, loop,
            )
            log.warning("reviewer_sweep: routed pr={} head={} reason=timeout", pr_number, head_sha[:8])
            continue
        if reason != "infrastructure":
            _route_cancelled_check(repo, pr_number, head_sha, workflow_run, "unrecognized", loop)
            continue
        context = check.get("name")
        run_id = workflow_run.get("id")
        if not isinstance(context, str) or not isinstance(run_id, int):
            _route_cancelled_check(repo, pr_number, head_sha, workflow_run, "workflow_run_malformed", loop)
            continue
        if not reviewer_gate_state.claim_required_context_rerun(pr_key, head_sha, context):
            _route_cancelled_check(repo, pr_number, head_sha, workflow_run, "rerun_already_attempted", loop)
            continue
        if github_reviewer_gate.rerun_failed_jobs(repo, run_id):
            _route_cancelled_check(repo, pr_number, head_sha, workflow_run, "rerun_requested", loop)
            continue
        _route_cancelled_check(repo, pr_number, head_sha, workflow_run, "rerun_request_failed", loop)


def _has_failed_job(workflow_run: dict[str, Any]) -> bool:
    """Return whether a failed sibling makes a cancelled run a fail-fast cascade."""
    return any(
        isinstance(job, dict) and str(job.get("conclusion") or "").lower() == "failure"
        for job in workflow_run.get("jobs") or []
    )


def _cancel_reason(workflow_run: dict[str, Any]) -> str:
    """Classify only the cancelled check run's annotation messages, fail closed by default."""
    messages = [
        str(annotation.get("message") or "")
        for annotation in workflow_run.get("annotations") or []
        if isinstance(annotation, dict)
    ]
    if any(message.startswith(_SUPERSESSION_PREFIX) for message in messages):
        return "supersession"
    if any(message.startswith(_TIMEOUT_PREFIX) for message in messages):
        return "timeout"
    if any(message.startswith(_DELIBERATE_CANCEL_PREFIX) for message in messages):
        return "deliberate"
    if any(message.startswith(prefix) for prefix in _INFRASTRUCTURE_ANNOTATION_PREFIXES for message in messages):
        return "infrastructure"
    return "unrecognized"


def _route_cancelled_check(
    repo: str,
    pr_number: int,
    head_sha: str,
    workflow_run: dict[str, Any],
    reason: str,
    loop: asyncio.AbstractEventLoop | None,
) -> None:
    """Wake the PR owner for a cancellation that cannot safely be retried."""
    github_reviewer_notifications.notify_required_check_cancelled(
        repo, pr_number, head_sha, workflow_run, reason, loop,
    )
    log.warning("reviewer_sweep: routed pr={} head={} reason={}", pr_number, head_sha[:8], reason)


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
