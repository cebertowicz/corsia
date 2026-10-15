"""Canonical Coordination-MVP launch scenarios as declared data.

Each :class:`Scenario` is the *contract* a later slice implements against: the
connector's raw payload, the normalized facts/identity/relationships Event Model
(S1) must produce, the resolution path Event Delivery (S4) must take, and the
delivery disposition (S4b) the recipient must receive. No behavior lives here —
only the declared shape, exactly as ``RFC-GENERIC-EVENT-ROUTING-BINDING`` says a
new connector should be *data, not a release*.

If a launch scenario cannot be expressed here as declared data, that is the S0
stop condition: report the exact scenario and why.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ── Canonical contract vocabulary (single source for fixtures + guards) ──────

#: The five — and ONLY five — Event Delivery dispositions (ED5 / AC-3.1).
DELIVERY_DISPOSITIONS: frozenset[str] = frozenset({
    "owner_wake",
    "owner_digest",
    "subscriber_wake",
    "subscriber_digest",
    "dead_letter",  # dead-letter / delivery-failure
})

#: User-attention / artifact-workflow concepts that MUST NOT become Event
#: Delivery dispositions (ED18, AC-3.1, AC-1.4). The taxonomy guard forbids
#: these as delivery outcomes.
FORBIDDEN_DELIVERY_CLASSES: frozenset[str] = frozenset({
    "user_raise", "raise", "warning", "decision", "report", "deliverable",
    "approve_to_user", "reject_with_reason", "answer_internally",
    "request_revision", "reroute_owner", "defer_until", "supersede",
    "notify_user", "badge",
})

#: Subscriber kinds on an ArtifactRecord (S2). ``parent_chain`` is digest-only.
SUBSCRIBER_KINDS: frozenset[str] = frozenset({"owner", "subscriber", "parent_chain"})

#: Resolution paths through the single ``resolve_targets`` function (S4 §1).
RESOLUTION_PATHS: frozenset[str] = frozenset({
    "path_a_resolve",              # registered artifact → owner+subscribers
    "path_b_declaration_bind",     # validated own-instance declaration binds first
    "path_b_rule_bind",            # no artifact → rule binds + registers
    "triage",             # no rule + ownerless_ok → scope/workspace triage owner
    "dead_letter",        # no owner + owner required → loud dead-letter
    "repair",             # equal-precedence/ambiguous → binder-repair artifact, not a guess
})

#: Connector tokens that may appear ONLY in declared-data / connector modules,
#: never in the generic routing path. Used by the no-bespoke-router guard.
CONNECTOR_TOKENS: frozenset[str] = frozenset({
    "gmail", "github", "check_suite", "pull_request", "issue_comment",
    "pull_request_review_comment", "check_run",
})


@dataclass(frozen=True)
class Scenario:
    """One launch scenario expressed purely as declared data."""

    name: str
    description: str
    connector: str                      # gmail | github | document | internal
    internal_event: str                 # normalized (product/domain) event type
    raw_payload: dict[str, Any]         # what the connector emits
    identity_facts: tuple[str, ...]     # IdentityFormula input fact keys (S1)
    expected_facts: dict[str, Any]      # normalized filterable facts
    relationships: tuple[dict[str, str], ...] = ()
    artifact_ref: dict[str, str] | None = None
    event_class: str = "informational"  # actionable | informational
    ownerless_ok: bool = False
    resolution: str = "path_a_resolve"
    disposition: str = "owner_wake"
    subscriber_kind: str = "owner"
    acs: tuple[str, ...] = ()
    slice_owner: str = "S1"
    notes: str = ""

    def __post_init__(self) -> None:
        # Self-check: a fixture can never declare an illegal contract value.
        assert self.disposition in DELIVERY_DISPOSITIONS, (
            f"{self.name}: disposition {self.disposition!r} not in taxonomy")
        assert self.disposition not in FORBIDDEN_DELIVERY_CLASSES, (
            f"{self.name}: disposition {self.disposition!r} is a user-attention class")
        assert self.resolution in RESOLUTION_PATHS, (
            f"{self.name}: resolution {self.resolution!r} unknown")
        assert self.subscriber_kind in SUBSCRIBER_KINDS, (
            f"{self.name}: subscriber_kind {self.subscriber_kind!r} unknown")


# ── Connector raw-payload builders (mirror real connector shapes) ────────────

def _gmail_payload(message_id: str, thread_id: str, sender: str,
                   label: str = "INBOX") -> dict[str, Any]:
    return {
        "message_id": message_id, "thread_id": thread_id,
        "from": sender, "to": "me@example.com", "label": label,
        "subject": "hello", "snippet": "first contact",
        # Screened, unflagged, needs the person now: the declared actionable rule.
        "screen_status": "screened", "injection": False, "needs_person": "now",
    }


def _github_pr_payload(repo: str, number: int, sha: str = "abc123") -> dict[str, Any]:
    return {
        "action": "opened", "repository": {"full_name": repo},
        "pull_request": {"number": number, "title": "Test PR",
                         "head": {"sha": sha}, "base": {"ref": "dev"}},
        "sender": {"login": "alice", "type": "User"},
    }


def _github_check_payload(repo: str, number: int, sha: str,
                          conclusion: str = "failure") -> dict[str, Any]:
    return {
        "action": "completed", "repository": {"full_name": repo},
        "check_suite": {"id": 9001, "conclusion": conclusion, "status": "completed",
                        "head_sha": sha,
                        "pull_requests": [{"number": number, "base": {"ref": "dev"},
                                           "head": {"sha": sha}}]},
        "sender": {"login": "ci[bot]", "type": "Bot"},
    }


def _doc_comment_payload(document_id: str, comment_id: str,
                         version: int = 3) -> dict[str, Any]:
    return {
        "document_id": document_id, "comment_id": comment_id,
        "author": "bob", "anchor": "para-2", "version": version,
        "body": "needs a citation",
    }


# ── The launch scenario set ──────────────────────────────────────────────────

SCENARIOS: dict[str, Scenario] = {}


def _add(scn: Scenario) -> Scenario:
    assert scn.name not in SCENARIOS, f"duplicate scenario {scn.name}"
    SCENARIOS[scn.name] = scn
    return scn


GMAIL_FIRST_TOUCH = _add(Scenario(
    name="gmail_first_touch",
    description="First-seen email; no pre-existing artifact; a rule binds+registers it.",
    connector="gmail", internal_event="email.received",
    raw_payload=_gmail_payload("msg-1", "thr-1", "user_1@example.com"),
    identity_facts=("message_id",),
    expected_facts={"from": "user_1@example.com", "label": "INBOX",
                    "thread_id": "thr-1", "message_id": "msg-1"},
    relationships=({"edge": "message_to_email_thread", "to_type": "email-thread",
                    "to_ref": "thr-1"},),
    artifact_ref={"integration": "gmail", "type": "email-thread", "external_id": "thr-1"},
    event_class="actionable", ownerless_ok=False,
    resolution="path_b_rule_bind", disposition="owner_wake",
    acs=("AC-1.6", "AC-2.2", "AC-3.2"), slice_owner="S3",
    notes="from=user_1@example.com -> project A registers + owns the thread.",
))

GMAIL_REPEAT = _add(Scenario(
    name="gmail_repeat",
    description="Later message on an already-registered email thread routes by ownership.",
    connector="gmail", internal_event="email.received",
    raw_payload=_gmail_payload("msg-2", "thr-1", "user_1@example.com"),
    identity_facts=("message_id",),
    expected_facts={"from": "user_1@example.com", "thread_id": "thr-1",
                    "message_id": "msg-2"},
    relationships=({"edge": "message_to_email_thread", "to_type": "email-thread",
                    "to_ref": "thr-1"},),
    artifact_ref={"integration": "gmail", "type": "email-thread", "external_id": "thr-1"},
    event_class="actionable",
    resolution="path_a_resolve", disposition="owner_digest",
    acs=("AC-2.1", "AC-1.6"), slice_owner="S4",
    notes="event_class is type-level (email.received=actionable); the digest vs "
          "wake split is a lifecycle decision in the S4 disposition table.",
))

GITHUB_PR = _add(Scenario(
    name="github_pr_opened",
    description="PR pre-registered by its worker; resolves to owner (PATH A).",
    connector="github", internal_event="pr.opened",
    raw_payload=_github_pr_payload("o/r", 7),
    identity_facts=("repo", "pr_number", "action"),
    expected_facts={"repo": "o/r", "pr_number": 7, "branch": "dev", "sha": "abc123"},
    relationships=(),
    artifact_ref={"integration": "github", "type": "pull-request", "external_id": "o/r#7"},
    event_class="actionable",
    resolution="path_a_resolve", disposition="owner_wake",
    acs=("AC-1.7", "AC-2.1", "AC-3.2"), slice_owner="S4",
))

GITHUB_CHECK = _add(Scenario(
    name="github_check_completed",
    description="check_suite -> PR relationship resolves to the PR's owner.",
    connector="github", internal_event="check.completed",
    raw_payload=_github_check_payload("o/r", 7, "abc123", conclusion="failure"),
    identity_facts=("repo", "check_suite_id", "conclusion", "head_sha"),
    expected_facts={"repo": "o/r", "head_sha": "abc123", "check_suite_id": 9001,
                    "conclusion": "failure"},
    relationships=({"edge": "check_to_pull_request", "to_type": "pull-request",
                    "to_ref": "o/r#7"},),
    artifact_ref={"integration": "github", "type": "pull-request", "external_id": "o/r#7"},
    event_class="actionable",
    resolution="path_a_resolve", disposition="owner_wake",
    acs=("AC-1.7", "AC-5.9", "AC-2.1"), slice_owner="S1",
    notes="Redelivered check resolves to the SAME event_id (identity formula).",
))

DOC_COMMENT = _add(Scenario(
    name="document_comment",
    description="Comment on an owned document; resolves to the doc's owner (PATH A).",
    connector="document", internal_event="comment.created",
    raw_payload=_doc_comment_payload("doc-9", "cmt-3", version=3),
    identity_facts=("comment_id", "version"),
    expected_facts={"document_id": "doc-9", "comment_id": "cmt-3", "author": "bob",
                    "anchor": "para-2", "version": 3},
    relationships=({"edge": "comment_to_document", "to_type": "document",
                    "to_ref": "doc-9"},),
    artifact_ref={"integration": "local", "type": "document", "external_id": "doc-9"},
    event_class="informational",
    resolution="path_a_resolve", disposition="owner_digest",
    acs=("AC-1.8", "AC-2.1"), slice_owner="S4",
))

PEER_CONFLICT_REPAIR = _add(Scenario(
    name="peer_conflict_repair",
    description="One object matches two equal-precedence peer-scope rules.",
    connector="gmail", internal_event="email.received",
    raw_payload=_gmail_payload("msg-x", "thr-x", "boss@example.com", label="labelB"),
    identity_facts=("message_id",),
    expected_facts={"from": "boss@example.com", "label": "labelB",
                    "thread_id": "thr-x", "message_id": "msg-x"},
    artifact_ref={"integration": "gmail", "type": "email-thread", "external_id": "thr-x"},
    event_class="actionable", ownerless_ok=False,
    resolution="repair", disposition="owner_wake",
    acs=("AC-2.6", "AC-1.6"), slice_owner="S3",
    notes="Equal precedence -> loud binder-repair artifact, never a delivery guess.",
))

PARENT_CHAIN_DIGEST = _add(Scenario(
    name="parent_chain_digest_only",
    description="A parent-chain subscriber receives digest and is NEVER woken.",
    connector="github", internal_event="pr.merged",
    raw_payload={"action": "closed", "repository": {"full_name": "o/r"},
                 "pull_request": {"number": 7, "title": "Test PR", "merged": True,
                                  "head": {"sha": "abc123"}, "base": {"ref": "dev"}},
                 "sender": {"login": "alice", "type": "User"}},
    identity_facts=("repo", "pr_number", "action"),
    expected_facts={"repo": "o/r", "pr_number": 7, "merged": True},
    artifact_ref={"integration": "github", "type": "pull-request", "external_id": "o/r#7"},
    event_class="informational",
    resolution="path_a_resolve", disposition="subscriber_digest",
    subscriber_kind="parent_chain",
    acs=("AC-3.5", "AC-3.1"), slice_owner="S4",
    notes="parent_chain -> DIGEST is structural in the disposition table; never wake chaining.",
))

RAISED_ARTIFACT_CLOSEOUT = _add(Scenario(
    name="raised_artifact_closeout",
    description="Clearing a raised attention item fans out across all subscribed levels.",
    connector="internal", internal_event="artifact.attention_cleared",
    raw_payload={"artifact_id": "art-5", "outcome": "answer",
                 "subscribed_levels": ["owner-thr", "parent-thr", "conductor-thr"]},
    identity_facts=("artifact_id", "outcome"),
    expected_facts={"artifact_id": "art-5", "outcome": "answer"},
    artifact_ref={"integration": "local", "type": "user-raise", "external_id": "art-5"},
    event_class="actionable",
    resolution="path_a_resolve", disposition="owner_wake",
    acs=("AC-4.3", "AC-4.12"), slice_owner="S6",
    notes="Closeout removes the attention item everywhere; does NOT archive artifact or subscription.",
))

REVIEW_PROFILE_FRESH = _add(Scenario(
    name="review_profile_fresh_run",
    description="An Artifact Review Profile runs fresh: artifact + profile criteria only.",
    connector="internal", internal_event="artifact.review_requested",
    raw_payload={"artifact_id": "art-6", "profile_id": "user-raise-default",
                 "artifact_snapshot": {"requested_action": "approve budget",
                                       "evidence": "...", "confidence": "low"}},
    identity_facts=("artifact_id", "profile_id"),
    expected_facts={"artifact_id": "art-6", "profile_id": "user-raise-default"},
    artifact_ref={"integration": "local", "type": "user-raise", "external_id": "art-6"},
    event_class="actionable",
    resolution="path_a_resolve", disposition="owner_wake",
    acs=("AC-4.10", "AC-4.11"), slice_owner="S5",
    notes="No parent conversation context injected (v1). Blocking failure prevents approve_to_user.",
))

DEFER_UNTIL_EXPIRY = _add(Scenario(
    name="defer_until_deadline_expiry",
    description="A deadline defer arms an Automation watch that fires once on expiry.",
    connector="internal", internal_event="automation.watch_expired",
    raw_payload={"artifact_id": "art-7",
                 "condition": {"type": "deadline", "value": "2026-06-27T09:00:00Z"},
                 "watch_id": "watch-1"},
    identity_facts=("watch_id", "artifact_id"),
    expected_facts={"artifact_id": "art-7", "watch_id": "watch-1"},
    artifact_ref={"integration": "local", "type": "user-raise", "external_id": "art-7"},
    event_class="actionable",
    resolution="path_a_resolve", disposition="owner_wake",
    acs=("AC-4.5", "AC-5.1"), slice_owner="S7",
    notes="Expiry routed by Event Delivery as a wake; Automation owns the watch lifecycle; fires once.",
))

DELIVERY_HEALTH_SINK = _add(Scenario(
    name="delivery_health_sink",
    description="A mechanism fault with no per-event owner reaches a live, non-archivable health sink.",
    connector="github", internal_event="check.completed",
    raw_payload=_github_check_payload("o/r", 999, "deadsha", conclusion="failure"),
    identity_facts=("repo", "check_suite_id", "conclusion", "head_sha"),
    expected_facts={"repo": "o/r", "head_sha": "deadsha", "conclusion": "failure"},
    artifact_ref={"integration": "github", "type": "pull-request", "external_id": "o/r#999"},
    event_class="actionable", ownerless_ok=False,
    resolution="dead_letter", disposition="dead_letter",
    acs=("AC-2.7", "AC-3.6"), slice_owner="S4",
    notes="Triage chain terminates at the non-archivable workspace health sentinel; never a clean no-op.",
))


# ── Cross-slice test pack (sequencing RFC §Cross-Slice Test Pack) ────────────
# Each entry names the scenario(s) a cross-slice contract test must exercise.

CROSS_SLICE_PACK: tuple[dict[str, Any], ...] = (
    {"id": "X1", "title": "Gmail first-touch -> rule-bind+register -> owner wake/digest",
     "scenario": "gmail_first_touch", "slice": "S3"},
    {"id": "X2", "title": "Gmail repeat -> artifact resolve, no binder-specific router",
     "scenario": "gmail_repeat", "slice": "S4"},
    {"id": "X3", "title": "GitHub PR/check -> artifact resolve path",
     "scenario": "github_check_completed", "slice": "S4"},
    {"id": "X4", "title": "Document comment -> artifact resolve path",
     "scenario": "document_comment", "slice": "S4"},
    {"id": "X5", "title": "Equal-precedence rule conflict -> repair artifact",
     "scenario": "peer_conflict_repair", "slice": "S3"},
    {"id": "X6", "title": "Parent-chain digest never wakes",
     "scenario": "parent_chain_digest_only", "slice": "S4"},
    {"id": "X7", "title": "Raised artifact closeout fan-out across subscribers",
     "scenario": "raised_artifact_closeout", "slice": "S6"},
    {"id": "X8", "title": "Review Profile fresh-context run + blocking-failure override",
     "scenario": "review_profile_fresh_run", "slice": "S5"},
    {"id": "X9", "title": "Deadline defer_until expiry through Automation watch",
     "scenario": "defer_until_deadline_expiry", "slice": "S7"},
    {"id": "X10", "title": "Delivery-health failure reaches a live health sink",
     "scenario": "delivery_health_sink", "slice": "S4"},
)


def scenario(name: str) -> Scenario:
    """Return a named scenario (raises KeyError if absent — fail loud)."""
    return SCENARIOS[name]


def all_scenarios() -> tuple[Scenario, ...]:
    """Every declared launch scenario."""
    return tuple(SCENARIOS.values())
