"""GitHub facts used by the reviewer gate's eligibility decision."""

from __future__ import annotations

import time
from typing import Any

from loguru import logger as log

_REVIEWER_CHECK_NAME = "qa/llm-reviewer"
_TERMINAL_GREEN_STATES = {"SUCCESS", "SKIPPED", "NEUTRAL"}
MERGEABILITY_POLL_ATTEMPTS = 8
MERGEABILITY_POLL_INTERVAL_S = 4.0
_TRANSIENT_MERGEABLE = "UNKNOWN"


def checks_green(
    repo: str,
    pr_number: int,
    head_sha: str,
    checks: list[dict[str, Any]] | None = None,
) -> bool | None:
    """Return True when every non-reviewer PR check is terminal-green.

    A reconciliation pass may supply the raw newest check runs it already read;
    the webhook path keeps its one-read behavior. None means checks are
    unreadable and False means they are readable but not yet eligible.
    """
    if checks is None:
        from reviewer.gh_helpers import get_check_runs_for_sha

        checks = get_check_runs_for_sha(repo, head_sha)
    if checks is None:
        log.warning(
            "reviewer-gate: PR #{} sha={} — check fetch unreadable "
            "(rate-limit or REST error)",
            pr_number, head_sha[:8],
        )
        return None
    if not checks:
        log.info("reviewer-gate: PR #{} sha={} — no check runs yet", pr_number, head_sha[:8])
        return False
    non_reviewer_checks = [
        check for check in checks if check.get("name") != _REVIEWER_CHECK_NAME
    ]
    if not non_reviewer_checks:
        log.info("reviewer-gate: PR #{} — only reviewer check is present, holding", pr_number)
        return False
    not_green = [
        check for check in non_reviewer_checks
        if str(check.get("state") or check.get("conclusion") or check.get("status") or "").upper()
        not in _TERMINAL_GREEN_STATES
    ]
    if not_green:
        log.info(
            "reviewer-gate: PR #{} — holding on non-green checks: {}",
            pr_number,
            [(check.get("name"), check.get("state") or check.get("conclusion")) for check in not_green],
        )
        return False
    return True


def fetch_open_pull_requests(repo: str, base_ref: str) -> list[dict[str, Any]] | None:
    """Read open pull requests for one target branch through the gate connector."""
    from reviewer.gh_reconciliation import get_open_pull_requests

    return get_open_pull_requests(repo, base_ref)


def fetch_required_check_contexts(repo: str, base_ref: str) -> set[str] | None:
    """Read required branch contexts rather than hard-coding merge policy."""
    from reviewer.gh_reconciliation import get_branch_required_contexts

    return get_branch_required_contexts(repo, base_ref)


def fetch_check_runs(repo: str, head_sha: str) -> list[dict[str, Any]] | None:
    """Read newest raw check runs so reconciliation can classify cancellations."""
    from reviewer.gh_reconciliation import get_check_runs

    return get_check_runs(repo, head_sha)


def reviewer_status(repo: str, head_sha: str) -> dict[str, str] | None:
    """Read the standing reviewer commit status for restart-safe de-duplication."""
    from reviewer.gh_helpers import get_commit_status_for_context

    return get_commit_status_for_context(repo, head_sha, _REVIEWER_CHECK_NAME)


def fetch_workflow_run(repo: str, check: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve the Actions run backing a cancelled required check."""
    from reviewer.gh_reconciliation import get_workflow_run_for_check

    return get_workflow_run_for_check(repo, check)


def rerun_workflow_run(repo: str, run_id: int) -> bool:
    """Re-run a GitHub Actions workflow through the supported Actions endpoint."""
    from reviewer.gh_reconciliation import rerun_workflow_run as rerun

    return rerun(repo, run_id)


def fetch_pr_info(repo: str, pr_number: int) -> dict[str, Any] | None:
    """Fetch reviewer-gate PR metadata through GitHub REST."""
    from reviewer.gh_helpers import get_pull_request_info

    pr_info = get_pull_request_info(repo, pr_number)
    if pr_info is None:
        log.warning("reviewer-gate: REST PR info fetch failed for #{}", pr_number)
    return pr_info


def fetch_pr_info_awaiting_mergeability(
    repo: str,
    pr_number: int,
    head_sha: str,
) -> dict[str, Any] | None:
    """Retry transient mergeability, which may be UNKNOWN after the final CI event.

    That final check-suite event may be the last trigger for this head (BUG #574),
    so exhausting the bounded retry is necessary to avoid leaving a ready PR clean
    but unreviewed. A draft is held without mergeability; polling it only wastes
    worker time.
    """
    pr_info: dict[str, Any] | None = None
    for attempt in range(MERGEABILITY_POLL_ATTEMPTS):
        pr_info = fetch_pr_info(repo, pr_number)
        if pr_info is None or pr_info["isDraft"]:
            return pr_info
        if not mergeability_is_unknown(pr_info):
            return pr_info
        if attempt + 1 < MERGEABILITY_POLL_ATTEMPTS:
            log.info(
                "reviewer-gate: PR #{} sha={} — mergeable=UNKNOWN, re-polling "
                "({}/{})",
                pr_number, head_sha[:8], attempt + 1, MERGEABILITY_POLL_ATTEMPTS,
            )
            time.sleep(MERGEABILITY_POLL_INTERVAL_S)
    return pr_info


def mergeability_is_unknown(pr_info: dict[str, Any]) -> bool:
    """Return whether GitHub has not resolved this pull request's mergeability."""
    mergeable = str(pr_info.get("mergeable") or _TRANSIENT_MERGEABLE).upper()
    return mergeable == _TRANSIENT_MERGEABLE


def hold_pr_until_reviewable(
    repo: str,
    pr_number: int,
    head_sha: str,
    pr_info: dict[str, Any],
) -> bool:
    """Hold review for a draft or a non-mergeable pull request."""
    from infra.product_events import emit_product_event

    if pr_info["isDraft"]:
        log.info(
            "reviewer-gate: PR #{} sha={} — draft PR, holding review",
            pr_number, head_sha[:8],
        )
        emit_product_event(
            "reviewer_gate", "draft_hold",
            properties={"repo": repo, "pr_number": pr_number, "head_sha": head_sha[:8]},
        )
        return True

    mergeable = pr_info.get("mergeable", "UNKNOWN")
    if mergeable == "MERGEABLE":
        return False

    log.info(
        "reviewer-gate: PR #{} sha={} — mergeable={!r}, holding review",
        pr_number, head_sha[:8], mergeable,
    )
    emit_product_event(
        "reviewer_gate", "not_mergeable",
        properties={"repo": repo, "pr_number": pr_number, "mergeable": mergeable},
    )
    return True
