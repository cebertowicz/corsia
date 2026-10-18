"""Fork PR approval state for the reviewer gate."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from loguru import logger as log

from events import reviewer_gate_state
from events.connectors import github_reviewer_gate
from infra.product_events import emit_product_event

_WAITING_TEXT = "fork head awaiting maintainer review label"
_APPROVED_TEXT = "fork head approved; review runs when checks are green"
_WORKFLOW_APPROVAL_FAILED_TEXT = "could not approve fork workflow runs; add review again"


def is_fork_pr_info(pr_info: dict[str, Any]) -> bool:
    """Treat a missing/deleted head repository as a fork conservatively."""
    head_repo = pr_info.get("headRepoFullName")
    base_repo = pr_info.get("baseRepoFullName")
    return (
        not isinstance(head_repo, str)
        or not head_repo
        or not isinstance(base_repo, str)
        or not base_repo
        or head_repo.casefold() != base_repo.casefold()
    )


def _publish_hold(repo: str, head_sha: str, text: str) -> None:
    """Publish the non-success presence status for a held fork head."""
    from reviewer.reviewer_categories import STATUS_PRESENCE_CONTEXT
    from reviewer.reviewer_status import set_status

    set_status(repo, head_sha, "pending", text, context=STATUS_PRESENCE_CONTEXT)


def holds_fork_review(
    repo: str, pr_number: int, head_sha: str, pr_info: dict[str, Any],
) -> bool:
    """Hold an unapproved fork head without entering the reviewer path."""
    if not is_fork_pr_info(pr_info):
        return False
    pr_key = reviewer_gate_state.pr_key(repo, pr_number)
    if reviewer_gate_state.get_approved_sha(pr_key) == head_sha:
        return False
    if reviewer_gate_state.get_reviewed_sha(pr_key) == head_sha:
        return False
    log.info("reviewer-gate: PR #{} sha={} — fork head awaits review label", pr_number, head_sha[:8])
    emit_product_event(
        "reviewer_gate", "fork_hold",
        properties={"repo": repo, "pr_number": pr_number, "head_sha": head_sha[:8]},
    )
    _publish_hold(repo, head_sha, _WAITING_TEXT)
    return True


def hold_fork_runs_awaiting_approval(repo: str, pr_number: int, head_sha: str) -> None:
    """Publish the fork hold while GitHub Actions waits for approval."""
    pr_info = github_reviewer_gate.fetch_pr_info(repo, pr_number)
    if pr_info is None:
        log.warning("reviewer-gate: PR #{} sha={} — held-run PR info unreadable", pr_number, head_sha[:8])
        emit_product_event(
            "reviewer_gate", "fork_hold_unreadable",
            properties={"repo": repo, "pr_number": pr_number, "head_sha": head_sha[:8]},
        )
        return
    holds_fork_review(repo, pr_number, head_sha, pr_info)


def approve_labeled_fork_head(
    repo: str,
    pr_number: int,
    head_sha: str,
    sender: str | None,
    internal_event: str,
    loop: Any,
    pr_info: dict[str, Any],
    evaluate_gate: Callable[..., None],
) -> None:
    """Record label approval for its exact fork head and re-evaluate the gate."""
    from reviewer.gh_helpers import approve_action_required_workflow_runs

    emit_product_event(
        "reviewer_gate", "review_label_approved",
        properties={
            "repo": repo,
            "pr_number": pr_number,
            "head_sha": head_sha[:8],
            "sender": sender,
        },
    )
    pr_key = reviewer_gate_state.pr_key(repo, pr_number)
    if reviewer_gate_state.get_reviewed_sha(pr_key) == head_sha:
        return
    if not approve_action_required_workflow_runs(repo, head_sha):
        emit_product_event(
            "reviewer_gate", "workflow_runs_approval_failed",
            properties={"repo": repo, "pr_number": pr_number, "head_sha": head_sha[:8]},
        )
        _publish_hold(repo, head_sha, _WORKFLOW_APPROVAL_FAILED_TEXT)
        return

    reviewer_gate_state.record_approval(pr_key, head_sha)
    _publish_hold(repo, head_sha, _APPROVED_TEXT)
    evaluate_gate(repo, head_sha, pr_number, internal_event, loop, pr_info=pr_info)
