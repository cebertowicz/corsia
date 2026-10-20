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


def checks_green(repo: str, pr_number: int, head_sha: str) -> bool | None:
    """Return True when every non-reviewer PR check is terminal-green.

    None means checks are unreadable and must be reported as retryable; False
    means checks are readable but pending, failing, or reviewer-only.
    """
    from reviewer.gh_helpers import get_check_runs_for_sha

    checks = None
    for attempt in range(MERGEABILITY_POLL_ATTEMPTS):
        checks = get_check_runs_for_sha(repo, head_sha)
        if checks is not None:
            break
        if attempt + 1 < MERGEABILITY_POLL_ATTEMPTS:
            log.info(
                "reviewer-gate: PR #{} sha={} — check runs unreadable, re-polling "
                "({}/{})",
                pr_number, head_sha[:8], attempt + 1, MERGEABILITY_POLL_ATTEMPTS,
            )
            time.sleep(MERGEABILITY_POLL_INTERVAL_S)
    if checks is None:
        log.warning(
            "reviewer-gate: PR #{} sha={} — check fetch unreadable "
            "(rate-limit or REST error)",
            pr_number, head_sha[:8],
        )
        return None

    if not checks:
        log.info(
            "reviewer-gate: PR #{} sha={} — no check runs yet",
            pr_number, head_sha[:8],
        )
        return False

    non_reviewer_checks = [
        check for check in checks
        if check.get("name") != _REVIEWER_CHECK_NAME
    ]
    if not non_reviewer_checks:
        log.info(
            "reviewer-gate: PR #{} — only reviewer check is present, holding",
            pr_number,
        )
        return False

    not_green = [
        check for check in non_reviewer_checks
        if str(check.get("state") or "").upper() not in _TERMINAL_GREEN_STATES
    ]
    if not_green:
        log.info(
            "reviewer-gate: PR #{} — holding on non-green checks: {}",
            pr_number,
            [(check.get("name"), check.get("state")) for check in not_green],
        )
        return False

    return True


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
    """Retry transient unreadable PR info or UNKNOWN mergeability after final CI.

    That final check-suite event may be the last trigger for this head (BUG #574),
    so exhausting the bounded retry is necessary to avoid leaving a ready PR clean
    but unreviewed. A draft is held without mergeability; polling it only wastes
    worker time.
    """
    pr_info: dict[str, Any] | None = None
    for attempt in range(MERGEABILITY_POLL_ATTEMPTS):
        pr_info = fetch_pr_info(repo, pr_number)
        if pr_info is not None and pr_info["isDraft"]:
            return pr_info
        if pr_info is not None and not mergeability_is_unknown(pr_info):
            return pr_info
        if attempt + 1 < MERGEABILITY_POLL_ATTEMPTS:
            if pr_info is None:
                log.info(
                    "reviewer-gate: PR #{} sha={} — PR info unreadable, re-polling "
                    "({}/{})",
                    pr_number, head_sha[:8], attempt + 1, MERGEABILITY_POLL_ATTEMPTS,
                )
            else:
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
