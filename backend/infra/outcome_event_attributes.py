"""Per-event attribute allowlists for ``outcome_events``.

The allowlist, not the sanitizer, is the control: each event name declares the
ids, counts and enums its attributes may carry, and everything else is dropped
and counted. Values that survive then pass the one canonical redactor,
``host_access.credential_sanitizer.sanitize_credential_data``; there is no
second redactor here. An event name with no entry keeps no attributes.

Registered so far: the test seed and the GitHub receipt mapping (spine §3.2,
events/connectors/github_outcomes.py); each later intake adds its own names
beside them.

An attribute whose value falls outside its registered set is dropped and counted,
never rewritten into a plausible-looking substitute: the row keeps no such attribute
and the event name's ``attribute_dropped`` total grows. That count is an aggregate
loss signal per event name — it does not record which row, or which value, was lost.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from core.correction_learning import (
    CAUSES as LEARNING_CAUSES,
    NEW_WORK_SIGNALS,
    OUTCOME_EVENT as LEARNING_OUTCOME_EVENT,
    TERMINAL_OUTCOMES,
    TIERS,
    TRIAGE_EVENT,
)
from core.corrections_recording import (
    CORRECTION_EVENT,
    FACT_KEYS as CORRECTION_FACT_KEYS,
    LABELS as CORRECTION_LABELS,
    SELF_CAUGHT_FACT_KEYS,
)
from core.size_rating import BASES, FACT_KEYS, SIZE_CLASSES, SIZE_EVENT
from host_access.credential_sanitizer import contains_credential, sanitize_credential_data
from infra.outcome_event_input import OutcomeInputRefusal

MAX_ATTRIBUTES_BYTES = 4 * 1024
_ID_RE = re.compile(r"[A-Za-z0-9_.:#/@+-]{1,200}")
# GitHub App bot logins add `[bot]` to the app slug. Only `author_login`
# accepts that suffix; owner claims, SHAs and keys retain the opaque-token shape.
_BOT_LOGIN_RE = re.compile(r"[A-Za-z0-9_.:#/@+-]{1,194}\[bot\]")


@dataclass(frozen=True)
class AttributeSpec:
    """One allowed attribute: an ``id``, a ``login``, a non-negative ``count`` or an ``enum``."""

    kind: str
    values: frozenset[str] = frozenset()
    max_chars: int = 0

    def accepts(self, value: Any) -> bool:
        """Whether ``value`` is a legal value for this attribute."""
        if self.kind == "count":
            return isinstance(value, int) and not isinstance(value, bool) and value >= 0
        if self.kind == "num":
            return (
                isinstance(value, (int, float)) and not isinstance(value, bool)
                and value >= 0 and value == value and value not in (float("inf"), float("-inf"))
                and round(value, 6) == value
            )
        if self.kind == "login":
            return (
                isinstance(value, str)
                and bool(_ID_RE.fullmatch(value) or _BOT_LOGIN_RE.fullmatch(value))
                and not contains_credential(value)
            )
        if self.kind == "id":
            return (
                isinstance(value, str) and _ID_RE.fullmatch(value) is not None
                and not contains_credential(value)
            )
        if self.kind == "text":
            return (
                isinstance(value, str) and bool(value.strip())
                and len(value) <= self.max_chars and "\x00" not in value
                and not contains_credential(value)
            )
        return isinstance(value, str) and value in self.values


def id_attr() -> AttributeSpec:
    """An opaque identifier: a short token of letters, digits and ``_.:#/@+-``."""
    return AttributeSpec("id")


def login_attr() -> AttributeSpec:
    """A GitHub account login: the opaque token shape, or a bot's ``<slug>[bot]``."""
    return AttributeSpec("login")


def count_attr() -> AttributeSpec:
    """A non-negative integer."""
    return AttributeSpec("count")


def enum_attr(*values: str) -> AttributeSpec:
    """One of a fixed set of strings."""
    return AttributeSpec("enum", frozenset(values))


def text_attr(max_chars: int) -> AttributeSpec:
    """A bounded, credential-free text field for an explicitly registered fact."""
    return AttributeSpec("text", max_chars=max_chars)


# A bounded union of documented workflow triggers and observed API values, including
# `dynamic`. Unknown values are dropped and counted; `none` means an absent key.
_ACTIONS_TRIGGER_EVENTS = frozenset({
    "branch_protection_rule", "check_run", "check_suite", "create", "delete",
    "deployment", "deployment_status", "discussion", "discussion_comment",
    "dynamic", "fork", "gollum", "image_version", "issue_comment", "issues",
    "label", "merge_group", "milestone", "none", "page_build", "public",
    "pull_request", "pull_request_review", "pull_request_review_comment",
    "pull_request_target", "push", "registry_package", "release",
    "repository_dispatch", "schedule", "status", "watch", "workflow_call",
    "workflow_dispatch", "workflow_run",
})

# GitHub check conclusions shared by run and suite rows; `none` is our absent-key sentinel.
_CONCLUSION_VALUES = (
    "success", "failure", "neutral", "cancelled", "timed_out",
    "action_required", "startup_failure", "stale", "skipped", "none",
)


_PR_ATTRS = {
    "repo": id_attr(), "pr": id_attr(), "base_ref": id_attr(), "head_sha": id_attr(),
}
# Observed and unverified: who opened the PR and what owner its body declares.
# The author is a GitHub login, which a bot writes with its `[bot]` suffix; the
# declared owner stays on the opaque-token shape because it comes from body text.
_PR_DECLARATION_ATTRS = {
    "author_login": login_attr(), "declared_owner_id": id_attr(),
    "declaration_count": count_attr(),
}
PRIORITY_VALUES = ("p0", "p1", "p2", "p3")
ISSUE_KIND_VALUES = ("bug", "feature", "enhancement", "documentation", "spec-update")

_ISSUE_ATTRS = {
    "repo": id_attr(), "issue": id_attr(), "label_count": count_attr(),
    "lane": id_attr(), "lane_count": count_attr(),
    "priority": enum_attr(*PRIORITY_VALUES),
    "kind": enum_attr(*ISSUE_KIND_VALUES),
}

ATTRIBUTE_ALLOWLISTS: dict[str, dict[str, AttributeSpec]] = {
    "test.outcome.recorded": {
        "ref_id": id_attr(),
        "count": count_attr(),
        "result": enum_attr("ok", "failed"),
    },
    # GitHub receipt mapping (spine §3.2); see events/connectors/github_outcomes.py.
    "github.pr.opened": {
        **_PR_ATTRS, **_PR_DECLARATION_ATTRS,
        "head_ref": id_attr(), "draft": enum_attr("yes", "no"),
    },
    "github.pr.pushed": {**_PR_ATTRS, "before_sha": id_attr()},
    "github.pr.reopened": {**_PR_ATTRS},
    "github.pr.closed": {**_PR_ATTRS},
    "github.pr.retargeted": {**_PR_ATTRS, "base_from": id_attr()},
    "github.pr.merged": {
        **_PR_ATTRS, **_PR_DECLARATION_ATTRS,
        "head_ref": id_attr(), "merge_sha": id_attr(), "minutes_since_opened": count_attr(),
        "reverts_sha": id_attr(), "reverts_pr": id_attr(),
    },
    "github.pr.introduced_by": {
        "repo": id_attr(), "pr": id_attr(), "introduced_by_pr": id_attr(),
        "confidence": enum_attr("stated"),
    },
    "github.pr.closes_issue": {
        "repo": id_attr(), "pr": id_attr(), "issue_repo": id_attr(),
        "issue": id_attr(), "confidence": enum_attr("stated"),
    },
    "github.pr.reverts": {
        "repo": id_attr(), "pr": id_attr(), "reverted_repo": id_attr(),
        "reverted_pr": id_attr(), "confidence": enum_attr("stated"),
    },
    "github.workflow_run.completed": {
        "repo": id_attr(), "run_id": id_attr(), "attempt": count_attr(),
        "workflow_id": id_attr(), "head_sha": id_attr(), "head_branch": id_attr(),
        "pr": id_attr(), "pr_count": count_attr(),
        "event": enum_attr(*_ACTIONS_TRIGGER_EVENTS),
        "conclusion": enum_attr(*_CONCLUSION_VALUES),
    },
    "github.check_suite.completed": {
        "repo": id_attr(), "suite_id": id_attr(), "head_sha": id_attr(),
        "pr": id_attr(), "head_branch": id_attr(), "app": id_attr(),
        "pr_count": count_attr(),
        "conclusion": enum_attr(*_CONCLUSION_VALUES),
    },
    # Estimated outcome size; core/size_rating.py owns the fact's keys, enums and
    # meaning. Ids, counts and enums only: no task text or hours.
    SIZE_EVENT: {
        **{key: id_attr() for key in FACT_KEYS},
        "node_revision": count_attr(),
        "basis": enum_attr(*BASES),
        "size_class": enum_attr(*SIZE_CLASSES),
    },
    # Local correction classifications retain only versioned result metadata,
    # never source-message text or an LLM explanation.
    CORRECTION_EVENT: {
        **{key: id_attr() for key in (set(CORRECTION_FACT_KEYS) | set(SELF_CAUGHT_FACT_KEYS))
           if key not in {"label", "threshold", "truncated", "original_tokens", "caught_by"}},
        "caught_by": enum_attr("person", "us"),
        "label": enum_attr(*CORRECTION_LABELS),
        "threshold": id_attr(),
        "truncated": enum_attr("true", "false"),
        "original_tokens": count_attr(),
    },
    TRIAGE_EVENT: {
        "tier": enum_attr(*TIERS), "cause": enum_attr(*LEARNING_CAUSES),
        "new_work": enum_attr("yes", "no"),
        "new_work_signal": enum_attr(*NEW_WORK_SIGNALS), "target_file": text_attr(500),
        "serious_proposed": enum_attr("yes", "no"), "serious_lowered_reason": text_attr(500),
    },
    LEARNING_OUTCOME_EVENT: {
        "outcome": enum_attr(*TERMINAL_OUTCOMES), "reason": text_attr(500),
        "lesson_id": id_attr(), "workflow_version": id_attr(),
    },
    "github.issue.opened": {**_ISSUE_ATTRS},
    "github.issue.reopened": {**_ISSUE_ATTRS},
    "github.issue.closed": {
        **_ISSUE_ATTRS, "state_reason": enum_attr("completed", "not_planned", "reopened", "duplicate"),
    },
    # Immutable verdict records use bounded serialized details rather than a
    # second table.  The engine writes these only while its switch is enabled.
    "measurement.verdict.planned": {"plan": text_attr(2_000)},
    "measurement.verdict.reached": {
        "verdict": enum_attr("keep", "revert"),
        "reason": text_attr(200), "detail": text_attr(2_000),
    },
}


def filter_attributes(
    event_name: str, attributes: dict[str, Any] | None,
    declared: dict[str, AttributeSpec] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Return (kept, dropped keys): allowlisted valid values only, sanitized.

    ``declared`` is a declared measure event's own specs, passed in by admission; without it the
    event's built-in allowlist applies, so what is kept never depends on what has been loaded.

    Raises ``OutcomeInputRefusal`` (a ``ValueError``) at the kept-attribute 4 KiB guard.

    An attribute outside a registered set is dropped, never rewritten into a
    substitute. Its key name comes back to the caller here, and the store turns that
    into one ``attribute_dropped`` total per event name: an aggregate loss signal.
    Which row lost the value, and what the value was, is recorded nowhere, so inside
    a row a dropped enum value is simply absent — only the moving counter says that
    anything was dropped at all.
    """
    allowed = declared if declared is not None else ATTRIBUTE_ALLOWLISTS.get(event_name, {})
    kept: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in (attributes or {}).items():
        spec = allowed.get(key) if isinstance(key, str) else None
        if spec is None or not spec.accepts(value):
            dropped.append(str(key))
        else:
            kept[key] = value
    kept = sanitize_credential_data(kept)
    if len(json.dumps(kept, separators=(",", ":")).encode("utf-8")) > MAX_ATTRIBUTES_BYTES:
        raise OutcomeInputRefusal(f"attributes for {event_name!r} exceed {MAX_ATTRIBUTES_BYTES} bytes")
    return kept, dropped
