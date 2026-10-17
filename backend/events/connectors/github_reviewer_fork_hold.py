"""Hold the LLM reviewer on fork pull request heads until a maintainer approves.

A fork head is reviewable only when its head commit has an approval row, which
only ``approve_labeled_head`` writes: a maintainer adds the review label, and
the label is consumed on use. A new push has a new head commit, so it is held
without anything being written at push time.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger as log

from events import reviewer_gate_state
from events.connectors import github_reviewer_gate
from infra.product_events import emit_product_event
from reviewer.gh_helpers import repo_full_name
from reviewer.reviewer_categories import STATUS_PRESENCE_CONTEXT

WAITING_TEXT = "approve the fork's workflow runs in GitHub, then add the review label (remove it first if present)"
APPROVED_TEXT = "head approved; the review runs once its checks pass"
UNVERIFIED_TEXT = "could not check the labeler's write access on GitHub; add the review label again (remove it first if present)"


def is_fork(head_repo: str | None, base_repo: str | None) -> bool:
    """Whether a head repository differs from its base; a missing side counts as a fork."""
    return not head_repo or not base_repo or head_repo.lower() != base_repo.lower()


def _side_repo(pr: dict[str, Any], side: str) -> str | None:
    return repo_full_name((pr.get(side) or {}).get("repo"))


def payload_is_fork(payload: dict[str, Any]) -> bool:
    """Whether a pull_request webhook payload describes a fork head."""
    pr = payload.get("pull_request") or {}
    base = _side_repo(pr, "base") or (payload.get("repository") or {}).get("full_name")
    return is_fork(_side_repo(pr, "head"), base)


def _reviewed(repo: str, pr_number: int, head_sha: str) -> bool:
    """Whether this head already has its final review, so its presence status must stay as it is."""
    return reviewer_gate_state.get_reviewed_sha(reviewer_gate_state.pr_key(repo, pr_number)) == head_sha


def _publish(repo: str, head_sha: str, text: str = WAITING_TEXT) -> None:
    """Show a non-success presence status so the head cannot merge unreviewed."""
    from reviewer.reviewer_status import set_status

    set_status(repo, head_sha, "pending", text, context=STATUS_PRESENCE_CONTEXT)


def holds_review(
    repo: str, pr_number: int, head_sha: str, pr_info: dict[str, Any],
) -> bool:
    """Hold a fork head no maintainer approved, and say what it waits for."""
    if not is_fork(pr_info.get("headRepoFullName"), pr_info.get("baseRepoFullName")):
        return False
    if reviewer_gate_state.get_approved_sha(reviewer_gate_state.pr_key(repo, pr_number)) == head_sha:
        return False
    if _reviewed(repo, pr_number, head_sha):
        return False
    log.info("reviewer-gate: PR #{} sha={} — fork head awaiting maintainer approval", pr_number, head_sha[:8])
    emit_product_event(
        "reviewer_gate", "fork_hold",
        properties={"repo": repo, "pr_number": pr_number, "head_sha": head_sha[:8]},
    )
    _publish(repo, head_sha)
    return True


def hold_runs_awaiting_approval(repo: str, pr_number: int, head_sha: str) -> None:
    """Say a fork head waits when GitHub holds its workflow runs, before any check is green."""
    pr_info = github_reviewer_gate.fetch_pr_info(repo, pr_number)
    if pr_info is None:
        log.warning("reviewer-gate: PR #{} sha={} — fork hold status not published, PR unreadable", pr_number, head_sha[:8])
        emit_product_event(
            "reviewer_gate", "fork_hold_unreadable",
            properties={"repo": repo, "pr_number": pr_number, "head_sha": head_sha[:8]},
        )
        return
    holds_review(repo, pr_number, head_sha, pr_info)


def approve_labeled_head(
    repo: str, pr_number: int, head_sha: str, sender: str | None, internal_event: str,
    loop: asyncio.AbstractEventLoop | None,
) -> None:
    """Approve the head a label was added on, for a sender with write access, then run the gate.

    The head is the one GitHub put in the label event, which is the head when
    the label was added, never a later read: a push after the label is held. A
    push that lands before the click is approved, so the label approves the head
    the PR shows at that moment, and its review still waits for that head's own
    checks to go green. The label is consumed either
    way, so adding it again always fires a new event. A permission GitHub could
    not answer approves nothing and is reported as unverified, not as a denial.
    A head already reviewed keeps its final presence status: the label does not
    re-run its review.
    """
    from events.reviewer_gate import on_pull_request_ready_for_review
    from reviewer.gh_fork_hold import remove_review_label, sender_can_approve

    approved = sender_can_approve(repo, sender)
    if approved:
        reviewer_gate_state.record_approval(reviewer_gate_state.pr_key(repo, pr_number), head_sha)
    label_removed = remove_review_label(repo, pr_number)
    outcome = {True: "approved", False: "denied", None: "unverified"}[approved]
    emit_product_event(
        "reviewer_gate", f"review_label_{outcome}",
        properties={
            "repo": repo, "pr_number": pr_number, "head_sha": head_sha[:8],
            "sender": sender, "label_removed": label_removed,
        },
    )
    if _reviewed(repo, pr_number, head_sha):
        return
    if approved is None:
        _publish(repo, head_sha, UNVERIFIED_TEXT)
    if approved:
        _publish(repo, head_sha, APPROVED_TEXT)
        on_pull_request_ready_for_review(
            repo=repo, head_sha=head_sha, pr_number=pr_number,
            internal_event=internal_event, loop=loop,
        )
