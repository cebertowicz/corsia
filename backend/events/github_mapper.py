"""Map GitHub webhooks to CORSIA internal event names."""

from __future__ import annotations

import logging
from typing import Any

from events import guard
from events.connectors.github import GITHUB_ROUTED_EVENTS, PrEnd, pr_end_state
from events.markers import (
    REVIEW_FP_MARKER_PREFIX as _REVIEW_FP_MARKER,
    REVIEW_RESOLVED_MARKER as _REVIEW_RESOLVED_MARKER,
)
from reviewer.reviewer_categories import LABEL_REVIEW

log = logging.getLogger(__name__)

# The connector owns the webhook vocabulary that declarations name. This mapper
# only maps payloads to those names. The rule that reads terminal state off a
# payload is the connector's ``events.connectors.github.pr_end_state``.
_PR_END_EVENTS: dict[PrEnd, str] = {
    "merged": GITHUB_ROUTED_EVENTS["pr.merged"],
    "closed": GITHUB_ROUTED_EVENTS["pr.closed"],
}
_PR_STATE_BY_EVENT: dict[str, PrEnd] = {
    event: state for state, event in _PR_END_EVENTS.items()
}
_LABEL_EVENTS = frozenset(
    GITHUB_ROUTED_EVENTS[f"{kind}.{action}"]
    for kind in ("pr", "issue") for action in ("labeled", "unlabeled")
)
LABEL_RULE_ONLY = "label_rule_only"


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


def label_wakes_nobody(internal_event: str, payload: dict[str, Any]) -> bool:
    """Whether this label change is heard by event rules alone.

    A label wakes no thread by default. The exception is a person adding the
    review label, which keeps its owner wake. The self-author check never stops
    a label from reaching rules, because every agent labels as the agents' account.
    """
    if internal_event not in _LABEL_EVENTS:
        return False
    review = (internal_event == GITHUB_ROUTED_EVENTS["pr.labeled"]
              and (payload.get("label") or {}).get("name") == LABEL_REVIEW)
    return not review or guard.is_bot_or_self_authored(payload)


def unruled_reason(internal_event: str, identity: Any, payload: dict[str, Any]) -> str | None:
    """Why an event no thread takes is parked, not dead-lettered: only rules route it."""
    if label_wakes_nobody(internal_event, payload):
        return LABEL_RULE_ONLY
    if identity is None and internal_event == GITHUB_ROUTED_EVENTS["issue.opened"]:
        return "issue_opened_unruled"
    return None


def _label_event(kind: str, action: str, payload: dict[str, Any]) -> str | None:
    """Any named label's change is an event; a change with no label name is not."""
    label = payload.get("label") or {}
    return GITHUB_ROUTED_EVENTS[f"{kind}.{action}"] if label.get("name") else None


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
            return GITHUB_ROUTED_EVENTS["pr.opened"]
        if action == "synchronize":
            return GITHUB_ROUTED_EVENTS["pr.synchronize"]
        if action == "ready_for_review":
            return GITHUB_ROUTED_EVENTS["pr.ready_for_review"]
        if action in ("labeled", "unlabeled"):
            return _label_event("pr", action, payload)
        if action == "closed":
            end = pr_end_state(payload)
            if end is None:
                # An unreadable close is not an event we can name; admission
                # ignores it rather than inventing a terminal state.
                return None
            return _PR_END_EVENTS[end]
        return None

    if event_type == "issues" and action == "opened":
        return GITHUB_ROUTED_EVENTS["issue.opened"]

    if event_type == "issues" and action in ("labeled", "unlabeled"):
        return _label_event("issue", action, payload)

    if event_type == "issue_comment" and action in ("created", "edited"):
        issue = payload.get("issue", {})
        on_pr = bool(issue.get("pull_request"))
        if on_pr and _is_noise_pr_comment(payload):
            return None
        return GITHUB_ROUTED_EVENTS[f"{'pr_comment' if on_pr else 'issue_comment'}.{action}"]

    if event_type == "pull_request_review_comment" and action in ("created", "edited"):
        if _is_noise_pr_comment(payload):
            return None
        return GITHUB_ROUTED_EVENTS[f"pr_comment.{action}"]

    if event_type == "pull_request_review" and action in ("submitted", "edited"):
        return GITHUB_ROUTED_EVENTS[f"review.{action}"]

    if event_type == "check_run":
        return None

    if event_type == "check_suite":
        if action == "completed":
            return GITHUB_ROUTED_EVENTS["check.completed"]
        return None

    return None
