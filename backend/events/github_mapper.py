"""Map GitHub webhooks to CORSIA internal event names."""

from __future__ import annotations

import logging
from typing import Any

from events.connectors.github import PrEnd, pr_end_state
from events.markers import (
    REVIEW_FP_MARKER_PREFIX as _REVIEW_FP_MARKER,
    REVIEW_RESOLVED_MARKER as _REVIEW_RESOLVED_MARKER,
)
from reviewer.reviewer_categories import LABEL_REVIEW

log = logging.getLogger(__name__)

# This module owns the webhook vocabulary: the name each terminal state maps to,
# and its inverse. The rule that reads the state off a payload is the connector's
# ``events.connectors.github.pr_end_state`` — never re-written here.
_PR_END_EVENTS: dict[PrEnd, str] = {
    "merged": "webhook.github.pr_merged",
    "closed": "webhook.github.pr_closed",
}
_PR_STATE_BY_EVENT: dict[str, PrEnd] = {
    event: state for state, event in _PR_END_EVENTS.items()
}


def ends_artifact(internal_event: str) -> bool:
    """Whether this webhook event is its pull request's end (merge or close)."""
    return internal_event in _PR_STATE_BY_EVENT


def pr_terminal_state(
    internal_event: str, payload: dict[str, Any], external_id: str
) -> PrEnd | None:
    """The terminal state one event name is entitled to write, or None to refuse.

    The webhook half of the router's source-fact guard: a terminal event name
    claims a state, and the payload must confirm it under the connector's one
    rule. A claim the payload does not confirm — a mapper defect, or a close
    whose ``merged`` fact is missing, null or non-bool — is refused here and
    said out loud with both values: dropping the write in silence is what lost
    #2381 its terminal state. A non-terminal event refuses without a warning.
    """
    expected = _PR_STATE_BY_EVENT.get(internal_event)
    if expected is None:
        return None
    pr_state = pr_end_state(payload)
    if pr_state != expected:
        log.warning(
            "github-webhook: refusing the terminal PR write for %s — event %s "
            "claims %s, its payload gives %s",
            external_id, internal_event, expected, pr_state,
        )
        return None
    return pr_state


def _is_bot_login(login: str) -> bool:
    return login.endswith("[bot]")


def _is_noise_pr_comment(payload: dict[str, Any]) -> bool:
    """Return true for known reviewer marker chatter."""
    comment = payload.get("comment") or {}
    body = comment.get("body") or ""
    sender = payload.get("sender") or comment.get("user") or {}
    login = sender.get("login") or ""
    is_bot = sender.get("type") == "Bot" or _is_bot_login(login)

    if not is_bot:
        return False

    if _REVIEW_FP_MARKER in body or _REVIEW_RESOLVED_MARKER in body:
        return True

    return False


def map_github_event(event_type: str, payload: dict[str, Any]) -> str | None:
    """Return the internal event name for a GitHub webhook payload."""
    action = payload.get("action", "")

    if event_type == "pull_request":
        if action == "opened":
            return "webhook.github.pr_opened"
        if action == "synchronize":
            return "webhook.github.pr_synchronize"
        if action == "ready_for_review":
            return "webhook.github.pr_ready_for_review"
        if action == "labeled":
            # Only the review label is an event; every other label stays unmapped.
            label = payload.get("label") or {}
            return "webhook.github.pr_labeled" if label.get("name") == LABEL_REVIEW else None
        if action == "closed":
            end = pr_end_state(payload)
            if end is None:
                # An unreadable close is not an event we can name; admission
                # ignores it rather than inventing a terminal state.
                return None
            return _PR_END_EVENTS[end]
        return None

    if event_type == "issue_comment":
        if action == "created":
            issue = payload.get("issue", {})
            if issue.get("pull_request"):
                if _is_noise_pr_comment(payload):
                    return None
                return "webhook.github.pr_comment"
            return "webhook.github.issue_comment"
        return None

    if event_type == "pull_request_review_comment":
        if action == "created":
            if _is_noise_pr_comment(payload):
                return None
            return "webhook.github.pr_comment"
        return None

    if event_type == "check_run":
        return None

    if event_type == "check_suite":
        if action == "completed":
            return "webhook.github.check_suite_completed"
        return None

    return None
