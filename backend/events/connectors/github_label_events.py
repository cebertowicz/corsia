"""One normalization of GitHub label and issue-lifecycle changes, whatever source read them.

The webhook translator, the repo-activity poller and the outcome mapper all call
``change_event`` (event rules §5.2, §5.3). The input is the webhook payload shape. The
poller builds that shape from an ``issues/events`` row (see
``github_issue_activity.row_delivery``), putting the row's own event time where a webhook
carries the item's ``updated_at``, and leaves out the item's label set, which a row does
not prove. A webhook change carries ``labels`` (the item's names after it); a poller
change has none.

The event_id is exact: ``IDENTITY`` below is the whole of it, with no matching window.
Which source delivered a change is settled once, by the poller reading the delivery log.

Identity is per change: two labels added to one PR head are two events, and the
same label removed and added again is two more, told apart by ``at``. Titles,
bodies and label descriptions are never facts.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from events.identity_formula import compute_event_id

CONNECTOR = "github"
_LABEL_ACTIONS = ("labeled", "unlabeled")
_ISSUE_ACTIONS = (*_LABEL_ACTIONS, "opened", "closed", "reopened")
#: Declared identity facts per type suffix. The connector manifest reads these, so a
#: declaration and the id computed here cannot drift apart.
IDENTITY: dict[str, tuple[str, ...]] = {
    "pr.labeled": ("repo", "pr_number", "label", "at"),
    "pr.unlabeled": ("repo", "pr_number", "label", "at"),
    "issue.labeled": ("repo", "issue_number", "label", "at"),
    "issue.unlabeled": ("repo", "issue_number", "label", "at"),
    "issue.opened": ("repo", "issue_number"),
    "issue.closed": ("repo", "issue_number", "at"),
    "issue.reopened": ("repo", "issue_number", "at"),
}


@dataclass(frozen=True)
class Change:
    """One normalized change: its declared type suffix, facts and ``event_id``."""

    type: str
    facts: dict[str, Any]
    event_id: str


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def label_names(labels: Any) -> list[str]:
    """The item's label names, sorted so two reads of one set agree."""
    names = {name for label in labels if (name := _text(_object(label).get("name")))} \
        if isinstance(labels, list) else set()
    return sorted(names)


def _minutes(created: Any, at: str) -> int | None:
    try:
        start = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        end = datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int((end - start).total_seconds() // 60))


def _issue_at(issue: dict[str, Any], action: str) -> str | None:
    field = {"opened": "created_at", "closed": "closed_at"}.get(action, "updated_at")
    return _text(issue.get(field))


def _shared(payload: dict[str, Any], item: dict[str, Any], number_fact: str, at: str) -> dict[str, Any] | None:
    repo, number = _text(_object(payload.get("repository")).get("full_name")), item.get("number")
    if repo is None or not isinstance(number, int) or isinstance(number, bool):
        return None
    facts: dict[str, Any] = {"repo": repo, number_fact: number, "at": at}
    if "labels" in item:
        facts["labels"] = label_names(item["labels"])
    if login := _text(_object(payload.get("sender")).get("login")):
        facts["actor"] = login
    if payload.get("action") in _LABEL_ACTIONS:
        label = _text(_object(payload.get("label")).get("name"))
        if label is None:
            return None
        facts["label"] = label
    return facts


def _pr_change(payload: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    action, pr = payload.get("action"), _object(payload.get("pull_request"))
    at = _text(pr.get("updated_at"))
    if action not in _LABEL_ACTIONS or at is None:
        return None
    facts = _shared(payload, pr, "pr_number", at)
    if facts is None:
        return None
    if sha := _text(_object(pr.get("head")).get("sha")):
        facts["sha"] = sha
    return f"pr.{action}", facts


def _issue_change(payload: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    action, issue = payload.get("action"), _object(payload.get("issue"))
    at = _issue_at(issue, str(action))
    if action not in _ISSUE_ACTIONS or at is None:
        return None
    facts = _shared(payload, issue, "issue_number", at)
    if facts is None:
        return None
    if action != "opened" and (minutes := _minutes(issue.get("created_at"), at)) is not None:
        facts["minutes_since_opened"] = minutes
    if action == "closed" and (reason := _text(issue.get("state_reason"))):
        facts["state_reason"] = reason
    return f"issue.{action}", facts


def change_event(event_type: str, payload: Any) -> Change | None:
    """The change a ``pull_request`` or ``issues`` payload describes, or ``None`` for any other."""
    if not isinstance(payload, dict) or event_type not in ("pull_request", "issues"):
        return None
    built = _pr_change(payload) if event_type == "pull_request" else _issue_change(payload)
    if built is None:
        return None
    suffix, facts = built
    return Change(suffix, facts, compute_event_id(CONNECTOR, suffix, facts, IDENTITY[suffix]))

