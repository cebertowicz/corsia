"""Publish finding returns and resolutions on existing GitHub review threads."""

from __future__ import annotations

import logging
from typing import Any

from infra.product_events import emit_product_event
from reviewer.reviewer_resolution import (
    post_resolve_reply,
    post_review_thread_reply,
    set_review_thread_resolved,
)
from reviewer.reviewer_threads import (
    THREAD_ACTION_KEY,
    ReviewThreadAction,
    format_threaded_comment_body,
)

log = logging.getLogger(__name__)


def reopen_and_republish(
    pr_number: int,
    repo: str,
    head_sha: str,
    finding: dict[str, Any],
) -> bool:
    """Reopen a prior finding thread and publish the current finding there."""
    action = finding.get(THREAD_ACTION_KEY)
    if not isinstance(action, ReviewThreadAction):
        return False
    reopened = (
        not action.was_resolved
        or set_review_thread_resolved(action.thread_id, resolved=False)
    )
    if action.was_resolved:
        _emit_lifecycle(pr_number, head_sha, action.thread_id, "reopen", reopened)
    if not reopened:
        return False

    posted = post_review_thread_reply(
        action.root_comment_id, pr_number, repo,
        format_threaded_comment_body(finding),
    )
    _emit_lifecycle(pr_number, head_sha, action.thread_id, "republish", posted)
    return posted


def publish_absence_reply(
    comment: dict[str, Any],
    repo: str,
    head_sha: str,
    pr_number: int,
) -> bool:
    """Post a truthful absence reply, then resolve its review thread if mapped.

    Returns whether the reply is on the PR. A failed resolve is logged and
    emitted, and the thread stays open, so the next run retries only the
    resolve; it does not make the declared reply missing.
    """
    action = comment.get(THREAD_ACTION_KEY)
    if not isinstance(action, ReviewThreadAction):
        return post_resolve_reply(comment, repo, head_sha)

    reply_posted = not action.reply_needed or post_resolve_reply(comment, repo, head_sha)
    if not reply_posted:
        return False
    resolved = set_review_thread_resolved(action.thread_id, resolved=True)
    _emit_lifecycle(pr_number, head_sha, action.thread_id, "resolve", resolved)
    return True


def _emit_lifecycle(
    pr_number: int,
    head_sha: str,
    thread_id: str,
    operation: str,
    succeeded: bool,
) -> None:
    """Record one attempted review-thread lifecycle transition."""
    outcome = "success" if succeeded else "failed"
    if not succeeded:
        log.error(
            "reviewer: thread lifecycle %s failed for PR #%d (%s)",
            operation, pr_number, thread_id,
        )
    emit_product_event(
        "reviewer", "thread_lifecycle",
        properties={
            "pr_number": pr_number,
            "head_sha": head_sha,
            "thread_id": thread_id,
            "operation": operation,
            "outcome": outcome,
        },
    )
