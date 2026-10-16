"""Hold the LLM reviewer on fork pull request heads until a maintainer approves.

A fork head is reviewable only when its head is the one a maintainer approved
by adding the review label; the label being present is not approval.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger as log

from events import reviewer_gate_state
from infra.product_events import emit_product_event
from reviewer.reviewer_categories import STATUS_PRESENCE_CONTEXT

WAITING_TEXT = "waiting for a maintainer to add the review label"
_SYNCHRONIZE = "webhook.github.pr_synchronize"


def is_fork(head_repo: str | None, base_repo: str | None) -> bool:
    """Whether a head repository differs from its base; a missing side counts as a fork."""
    return not head_repo or not base_repo or head_repo.lower() != base_repo.lower()


def _side_repo(pr: dict[str, Any], side: str) -> str | None:
    repo = (pr.get(side) or {}).get("repo")
    name = repo.get("full_name") if isinstance(repo, dict) else None
    return name if isinstance(name, str) and name else None


def payload_is_fork(payload: dict[str, Any]) -> bool:
    """Whether a pull_request webhook payload describes a fork head."""
    pr = payload.get("pull_request") or {}
    base = _side_repo(pr, "base") or (payload.get("repository") or {}).get("full_name")
    return is_fork(_side_repo(pr, "head"), base)


def _publish_waiting(repo: str, head_sha: str) -> None:
    """Show a non-success presence status so the waiting head cannot merge."""
    from reviewer.reviewer_status import set_status

    set_status(repo, head_sha, "pending", WAITING_TEXT, context=STATUS_PRESENCE_CONTEXT)


def holds_review(
    repo: str, pr_number: int, head_sha: str, pr_info: dict[str, Any],
) -> bool:
    """Hold a fork head the maintainer has not approved, and say so on its status."""
    if not is_fork(pr_info.get("headRepoFullName"), pr_info.get("baseRepoFullName")):
        return False
    pr_key = reviewer_gate_state.pr_key(repo, pr_number)
    if reviewer_gate_state.get_approved_sha(pr_key) == head_sha:
        return False
    log.info("reviewer-gate: PR #{} sha={} — fork head awaiting maintainer approval", pr_number, head_sha[:8])
    emit_product_event(
        "reviewer_gate", "fork_hold",
        properties={"repo": repo, "pr_number": pr_number, "head_sha": head_sha[:8]},
    )
    _publish_waiting(repo, head_sha)
    return True


def on_fork_push(
    repo: str, pr_number: int, head_sha: str, internal_event: str, payload: dict[str, Any],
) -> None:
    """Hold a fork head the moment it opens or changes.

    A new push drops the earlier approval and the review label, so approving
    the new head takes a fresh label event.
    """
    if not repo or not pr_number or not head_sha or not payload_is_fork(payload):
        return
    if internal_event == _SYNCHRONIZE:
        from reviewer.gh_fork_hold import remove_review_label

        reviewer_gate_state.clear_approval(reviewer_gate_state.pr_key(repo, pr_number))
        remove_review_label(repo, pr_number)
    _publish_waiting(repo, head_sha)


def approve_head(
    repo: str, pr_number: int, head_sha: str, sender: str | None, internal_event: str,
    loop: asyncio.AbstractEventLoop | None,
) -> None:
    """Record the head a maintainer approved, release its held CI runs, and re-evaluate the gate."""
    from events.reviewer_gate import on_pull_request_ready_for_review
    from reviewer.gh_fork_hold import approve_waiting_runs

    reviewer_gate_state.record_approval(reviewer_gate_state.pr_key(repo, pr_number), head_sha)
    approved_runs = approve_waiting_runs(repo, head_sha)
    emit_product_event(
        "reviewer_gate", "review_label_approved",
        properties={
            "repo": repo, "pr_number": pr_number, "head_sha": head_sha[:8],
            "sender": sender, "approved_runs": approved_runs,
        },
    )
    on_pull_request_ready_for_review(
        repo=repo, head_sha=head_sha, pr_number=pr_number,
        internal_event=internal_event, loop=loop,
    )
