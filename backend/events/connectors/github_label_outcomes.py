"""Outcome drafts for GitHub label changes and issue reopens, from the shared normalization.

``github_outcomes`` hands a ``pull_request`` or ``issues`` payload here for the actions
it does not map itself. Facts and identity come from ``github_label_events.change_event``.
Label names, logins and counts only; the item's whole label set stays on the routed event,
not in the row. A change carrying no ``labels`` (a poller copy) records no label-derived
attributes (count, priority, lane, kind).
"""
from __future__ import annotations

from typing import Any

from events.connectors import github_label_events as events
from events.connectors import github_outcome_input as shape
from events.connectors.github_outcomes import OutcomeDraft, label_attributes

_ISSUE_ACTIONS = ("labeled", "unlabeled", "reopened")


def label_drafts(event_type: str, payload: dict[str, Any], repo: str) -> list[OutcomeDraft]:
    """The drafts one ``labeled``, ``unlabeled`` or issue ``reopened`` delivery carries; else ``[]``."""
    action = payload.get("action")
    if event_type == "pull_request" and action not in ("labeled", "unlabeled"):
        return []
    if event_type == "issues" and action not in _ISSUE_ACTIONS:
        return []
    change = events.change_event(event_type, payload)
    if change is None:
        return []
    facts = change.facts
    at = shape.timestamp(facts["at"])
    label = facts.get("label")
    if event_type == "pull_request":
        subject = f"github:pr:{repo}#{facts['pr_number']}"
        attrs: dict[str, Any] = {"repo": repo, "pr": str(facts["pr_number"])}
        if "labels" in facts:
            attrs["label_count"] = len(facts["labels"])
        if "sha" in facts:
            attrs["head_sha"] = facts["sha"]
    else:
        subject = f"github:issue:{repo}#{facts['issue_number']}"
        attrs = {"repo": repo, "issue": str(facts["issue_number"])}
        if "labels" in facts:
            attrs.update(label_attributes(payload["issue"]["labels"]))
        if "minutes_since_opened" in facts:
            attrs["minutes_since_opened"] = facts["minutes_since_opened"]
    if label is not None:
        attrs["label"] = label
    if "actor" in facts:
        attrs["actor"] = facts["actor"]
    return [OutcomeDraft(f"{subject}:{change.type}:{label or ''}:{at}", f"github.{change.type}", at, subject, attrs)]
