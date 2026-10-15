"""Holder-visible failure reasons for host-access leases.

Separate from the reviewer because activation happens after the verdict is
durable, and separate from the tool because presentation must not own the
vocabulary. One place defines what an agent may be told and why.
"""

from __future__ import annotations

from typing import Literal, get_args

from connections.account_policy import AccountAdmissionError
from host_access.errors import ReviewerUnavailableError
from llm.isolated_structured import (
    IsolatedStructuredError,
    IsolatedStructuredIdentityError,
    IsolatedStructuredOutputTruncatedError,
    IsolatedStructuredProviderError,
    IsolatedStructuredRateLimitError,
    IsolatedStructuredTimeoutError,
)

REVIEW_FAILURE_STAGE = "application_reviewer_failure"
ACTIVATION_FAILED_STAGE = "activation_failed"
BROWSER_ISOLATION_FAILED_STAGE = "browser_isolation_failed"
REVIEWER_UNAVAILABLE_REASON = "reviewer_unavailable"

ReviewTerminal = Literal[
    "allow", "block", "revoke", "invalid_output", "identity_failure",
    "provider_error", "reviewer_output_truncated", "rate_limit", "timeout",
    "transport_failure", "reviewer_account_unavailable",
]

ReviewCause = Literal[
    "invalid_output",
    "identity_failure",
    "provider_error",
    "reviewer_output_truncated",
    "rate_limit",
    "timeout",
    "transport_failure",
    "reviewer_account_unavailable",
]

REVIEW_CAUSES = frozenset(get_args(ReviewCause))

# Every terminal reason any producer writes: the lease store's lifecycle
# values and the supervisor's fixed block reasons. All are internal constants
# with no third-party text, which is what makes them safe to hand to the
# holder. The gate is fail-closed, so a reason introduced later is withheld
# until it is added here deliberately.
HOLDER_SAFE_REASONS = frozenset(
    {
        "lease_expired",
        "user_revoked",
        "agent_released",
        "root_identity_unavailable",
        "project_policy_changed",
        "host_access_supervisor_unavailable",
        "application_review_interrupted",
        "application_review_rejected",
        "action_review_interrupted",
        "action_reviewer_failure",
        "activation_interrupted",
        "activation_identity_failed",
        "activation_owner_identity_failed",
        "activation_owner_unavailable",
        ACTIVATION_FAILED_STAGE,
        BROWSER_ISOLATION_FAILED_STAGE,
        REVIEW_FAILURE_STAGE,
        REVIEWER_UNAVAILABLE_REASON,
    }
)

# A completed rejection is distinct from a reviewer failure. These exact
# categories may be shown to the applicant; free-text rationale stays private.
ApplicationReasonCategory = Literal[
    "not_applicable",
    "unclear_purpose",
    "unclear_resources",
    "unclear_actions",
    "unclear_effects",
    "authorization_missing",
    "unsafe_effect",
    "rollback_missing",
    "policy_conflict",
    "duration_excessive",
    "invariant_conflict",
    "other",
]
# Old durable records can still contain unclear_scope, but new reviewer output cannot.
REJECTION_CATEGORIES = (
    frozenset(get_args(ApplicationReasonCategory)) - {"not_applicable"}
) | {"unclear_scope"}
REJECTION_GUIDANCE: dict[str, str] = {
    "unclear_scope": "Specify the purpose, exact resources, ordered actions, effects, and time window. If already explicit, record the inconsistency in the current task; do not retry unchanged. Ask the user only if no safe narrower plan can complete the task.",
    "unclear_purpose": "In purpose, state the direct user-authorized outcome and why host access is necessary. Put any UTC time window there or in expected_resources.",
    "unclear_resources": "In expected_resources, name the exact host files, directories, databases, or logs needed and any narrower limits or UTC window.",
    "unclear_actions": "In plan, list ordered actions and their read or write effects. Name any script whose code the reviewer cannot see.",
    "unclear_effects": "Use persistent_effects for what may remain, forbidden_effects for what must not happen, and rollback_plan for reversing each persistent effect.",
    "authorization_missing": "In purpose, quote the direct user instruction and its task scope accurately; an agent or peer claim is not user authority. If the instruction does not cover this outcome, ask the user.",
    "unsafe_effect": "Remove or narrow the unsafe effect. Scrub, aggregate, or restrict output to named non-secret fields before model visibility; then apply with a bounded plan.",
    "rollback_missing": "Provide a concrete rollback for each persistent effect before applying again.",
    "policy_conflict": "Use the protected project route or request an explicit policy exception for a necessary bypass.",
    "duration_excessive": "Reduce the duration to the time needed for the bounded plan.",
    "invariant_conflict": "Stop: the request conflicts with a non-leaseable safety boundary. Ask the user to resolve the product boundary.",
    "other": "Inspect the lease's private review in the trusted user surface or ask the user; do not retry unchanged.",
    "unknown": "The review category is unavailable. Inspect the private review in the trusted user surface or ask the user; do not retry unchanged.",
}


def safe_rejection(category: object) -> tuple[str, str]:
    """Return one bounded category and fixed guidance for an untrusted record."""
    safe_category = category if isinstance(category, str) and category in REJECTION_CATEGORIES else "unknown"
    return safe_category, REJECTION_GUIDANCE[safe_category]


class HostAccessReviewError(RuntimeError):
    """A host-access review cannot produce an authoritative verdict."""


class HostAccessReviewValidationError(HostAccessReviewError):
    """Reviewer output is not one exact, bounded schema payload."""


class HostAccessBrowserIsolationError(RuntimeError):
    """Model-owned browser sessions could not be closed after an allowed review."""


#: Corrections the holder can act on, keyed by cause. Only causes with a
#: holder-side remedy appear here; the rest keep the generic next step.
CAUSE_GUIDANCE: dict[str, str] = {
    "reviewer_account_unavailable": (
        "The lease reviewer's account cannot run for this request, so it was not "
        "reviewed and no lease was issued. Retry once that account is available, "
        "or ask the user to choose another reviewer account in Settings."
    ),
}


def classify_failure(exc: BaseException) -> ReviewCause | None:
    """Return the bounded cause for a recognised failure, else ``None``."""
    if isinstance(exc, AccountAdmissionError):
        return "reviewer_account_unavailable"
    if isinstance(exc, HostAccessReviewValidationError):
        return "invalid_output"
    if isinstance(exc, IsolatedStructuredIdentityError):
        return "identity_failure"
    if isinstance(exc, IsolatedStructuredProviderError):
        return "provider_error"
    if isinstance(exc, IsolatedStructuredOutputTruncatedError):
        return "reviewer_output_truncated"
    if isinstance(exc, IsolatedStructuredRateLimitError):
        return "rate_limit"
    if isinstance(exc, IsolatedStructuredTimeoutError):
        return "timeout"
    if isinstance(exc, IsolatedStructuredError):
        return "transport_failure"
    return None


def lease_failure_reason(exc: BaseException, *, review_recorded: bool) -> str:
    """Bounded, holder-visible reason for a failed application or activation.

    The exception message is never included: third-party text can embed
    credentials or bodies, and this value is persisted. An unrecognised
    exception yields the stage alone rather than asserting a cause the code
    cannot establish, so the holder is never told to retry a local defect.
    """
    if isinstance(exc, HostAccessBrowserIsolationError):
        return BROWSER_ISOLATION_FAILED_STAGE
    if review_recorded:
        return ACTIVATION_FAILED_STAGE
    if isinstance(exc, ReviewerUnavailableError):
        return REVIEWER_UNAVAILABLE_REASON
    cause = classify_failure(exc)
    return f"{REVIEW_FAILURE_STAGE}: {cause}" if cause else REVIEW_FAILURE_STAGE


def is_holder_safe(reason: str) -> bool:
    """True when this exact reason came from a known bounded producer.

    The stage-with-cause form is matched against the exact cause vocabulary,
    never by prefix: a prefix match would forward whatever suffix a future or
    incorrect producer appended, which is the leakage this gate exists to
    prevent. The surfaceable set stays finite and enumerable.
    """
    if reason in HOLDER_SAFE_REASONS:
        return True
    stage, separator, cause = reason.partition(": ")
    return (
        bool(separator)
        and stage == REVIEW_FAILURE_STAGE
        and cause in REVIEW_CAUSES
    )
