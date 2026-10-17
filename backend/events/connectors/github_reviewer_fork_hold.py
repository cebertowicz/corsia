"""Hold the LLM reviewer on fork pull request heads until a maintainer approves.

A fork head is reviewable only when it is the head a maintainer approved by
adding the review label; the label itself is only the trigger. The approval
row, the presence status, the label and the CI runs GitHub holds for a fork are
written only here, by ``reconcile_push``, ``reconcile_label`` and
``holds_review``, one at a time per pull request.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from loguru import logger as log

from events import reviewer_gate_state
from events.connectors import github_reviewer_gate
from infra.product_events import emit_product_event
from reviewer.gh_helpers import repo_full_name
from reviewer.reviewer_categories import LABEL_REVIEW, STATUS_PRESENCE_CONTEXT

WAITING_TEXT = "waiting for a maintainer to add the review label"
STALE_LABEL_TEXT = "waiting: a maintainer must remove the review label and add it again"
APPROVED_TEXT = "approved by a maintainer, waiting for checks"
RUNS_HELD_TEXT = "approved, but a maintainer must approve the fork's workflow runs in Actions"
_SYNCHRONIZE = "webhook.github.pr_synchronize"

_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


def _pr_lock(pr_key: str) -> threading.Lock:
    """The lock that serializes every fork-hold write for one pull request."""
    with _locks_guard:
        return _locks.setdefault(pr_key, threading.Lock())


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


def _publish(repo: str, head_sha: str, text: str) -> None:
    """Show a non-success presence status so the head cannot merge unreviewed."""
    from reviewer.reviewer_status import set_status

    set_status(repo, head_sha, "pending", text, context=STATUS_PRESENCE_CONTEXT)


def _has_review_label(pr_info: dict[str, Any]) -> bool:
    labels = pr_info.get("labels") or []
    return any(isinstance(label, dict) and label.get("name") == LABEL_REVIEW for label in labels)


def holds_review(
    repo: str, pr_number: int, head_sha: str, pr_info: dict[str, Any],
) -> bool:
    """Hold a fork head the maintainer has not approved, and say what it waits for."""
    if not is_fork(pr_info.get("headRepoFullName"), pr_info.get("baseRepoFullName")):
        return False
    pr_key = reviewer_gate_state.pr_key(repo, pr_number)
    with _pr_lock(pr_key):
        if reviewer_gate_state.get_approved_sha(pr_key) == head_sha:
            return False
        log.info("reviewer-gate: PR #{} sha={} — fork head awaiting maintainer approval", pr_number, head_sha[:8])
        emit_product_event(
            "reviewer_gate", "fork_hold",
            properties={"repo": repo, "pr_number": pr_number, "head_sha": head_sha[:8]},
        )
        # A label on an unapproved head can never fire a new labeled event.
        _publish(repo, head_sha, STALE_LABEL_TEXT if _has_review_label(pr_info) else WAITING_TEXT)
    return True


def _current_head(repo: str, pr_number: int) -> tuple[dict[str, Any] | None, str | None]:
    """The pull request's metadata and head as GitHub reports them now, not as an event ordered them."""
    pr_info = github_reviewer_gate.fetch_pr_info(repo, pr_number)
    return pr_info, (pr_info or {}).get("headRefOid") or None


def reconcile_push(
    repo: str, pr_number: int, head_sha: str, internal_event: str, payload: dict[str, Any],
) -> None:
    """Hold a fork head the moment it opens or changes.

    A new push drops the earlier approval and the review label, so approving
    the new head takes a fresh label event. An event for a head that is no
    longer current, or already approved, changes nothing.
    """
    if not repo or not pr_number or not head_sha or not payload_is_fork(payload):
        return
    pr_key = reviewer_gate_state.pr_key(repo, pr_number)
    with _pr_lock(pr_key):
        _, current = _current_head(repo, pr_number)
        approved = reviewer_gate_state.get_approved_sha(pr_key)
        if (current and current != head_sha) or approved == head_sha:
            log.info("fork hold: {}#{} push event for {} is stale or already approved", repo, pr_number, head_sha[:8])
            return
        label_cleared = True
        if internal_event == _SYNCHRONIZE:
            from reviewer.gh_fork_hold import remove_review_label

            if approved:
                reviewer_gate_state.clear_approval(pr_key)
            label_cleared = remove_review_label(repo, pr_number)
        _publish(repo, head_sha, WAITING_TEXT if label_cleared else STALE_LABEL_TEXT)


def _deny(repo: str, pr_number: int, head_sha: str, sender: str | None, approved: bool) -> None:
    """Ignore a label from someone without write access: remove it and keep the head held."""
    from reviewer.gh_fork_hold import remove_review_label

    removed = remove_review_label(repo, pr_number)
    emit_product_event(
        "reviewer_gate", "review_label_denied",
        properties={"repo": repo, "pr_number": pr_number, "head_sha": head_sha[:8], "sender": sender},
    )
    if not approved:
        _publish(repo, head_sha, WAITING_TEXT if removed else STALE_LABEL_TEXT)


def _approve_locked(
    repo: str, pr_number: int, head_sha: str, sender: str | None,
) -> bool:
    """Approve the current head for a sender with write access; True when it was approved."""
    from reviewer.gh_fork_hold import approve_waiting_runs, sender_can_approve

    pr_key = reviewer_gate_state.pr_key(repo, pr_number)
    pr_info, current = _current_head(repo, pr_number)
    if pr_info is None or not current:
        log.warning("fork hold: {}#{} label not honored, current head unreadable", repo, pr_number)
        _publish(repo, head_sha, STALE_LABEL_TEXT)
        return False
    if not is_fork(pr_info.get("headRepoFullName"), pr_info.get("baseRepoFullName")) or current != head_sha:
        return False
    approved = reviewer_gate_state.get_approved_sha(pr_key) == head_sha
    if not sender_can_approve(repo, sender):
        _deny(repo, pr_number, head_sha, sender, approved)
        return False
    reviewer_gate_state.record_approval(pr_key, head_sha)
    runs = approve_waiting_runs(repo, head_sha)
    released = runs is not None and runs[1] == 0
    _publish(repo, head_sha, APPROVED_TEXT if released else RUNS_HELD_TEXT)
    emit_product_event(
        "reviewer_gate", "review_label_approved",
        properties={
            "repo": repo, "pr_number": pr_number, "head_sha": head_sha[:8], "sender": sender,
            "approved_runs": runs[0] if runs else None, "failed_runs": runs[1] if runs else None,
        },
    )
    return True


def reconcile_label(
    repo: str, pr_number: int, head_sha: str, sender: str | None, internal_event: str,
    loop: asyncio.AbstractEventLoop | None,
) -> None:
    """Approve the head a maintainer labelled, then re-evaluate the gate for it."""
    from events.reviewer_gate import on_pull_request_ready_for_review

    with _pr_lock(reviewer_gate_state.pr_key(repo, pr_number)):
        approved = _approve_locked(repo, pr_number, head_sha, sender)
    if approved:
        on_pull_request_ready_for_review(
            repo=repo, head_sha=head_sha, pr_number=pr_number,
            internal_event=internal_event, loop=loop,
        )
