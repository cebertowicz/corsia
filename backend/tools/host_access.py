"""Agent-facing request for and release of reviewed temporary host access."""

from __future__ import annotations

import json
from typing import Any

from host_access.failure_reasons import (
    ACTIVATION_FAILED_STAGE,
    BROWSER_ISOLATION_FAILED_STAGE,
    CAUSE_GUIDANCE,
    REVIEWER_UNAVAILABLE_REASON,
    is_holder_safe,
    safe_rejection,
)
from host_access.reviewer_target import REVIEWER_UNAVAILABLE_ERROR_KEY
from host_access.reviewer_unavailable import (
    application_guidance,
)
from host_access.supervisor import (
    release_requested_lease,
    request_reviewed_lease,
)
from infra.host_access_leases import (
    MAX_LEASE_DURATION_SECONDS,
    MAX_PLAN_STEP_CHARS,
    MAX_PLAN_STEPS,
    MAX_PURPOSE_CHARS,
    MAX_RISK_FIELD_CHARS,
    MAX_POLICY_EXCEPTIONS,
    LeaseError,
)
from tools.base import Tool
from tools.host_access_exception_schema import policy_exception_schema


class ReleaseHostAccessTool(Tool):
    """End this thread's own lease early so a new scope can be applied for."""

    @property
    def name(self) -> str:
        return "release_host_access"

    @property
    def description(self) -> str:
        return (
            "End this thread's own temporary host-access lease immediately, "
            "before it expires. Use it when the approved purpose is finished, "
            "or when the work now needs a different scope: call "
            "release_host_access({}), then submit a new request_host_access "
            "application describing the new scope. Releasing only gives up access; it "
            "never grants any."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": [],
            "additionalProperties": False,
            "properties": {},
        }

    async def execute(self, **kwargs: Any) -> str:
        thread_id = kwargs.pop("_thread_id", None)
        kwargs.pop("_thread_project_id", None)
        kwargs.pop("_host_access_turn_identity", None)
        if not thread_id:
            return json.dumps(
                {
                    "error": "host_access_release_unattributed",
                    "released": False,
                },
                separators=(",", ":"),
            )
        try:
            lease = await release_requested_lease(thread_id)
        except LeaseError as exc:
            return json.dumps(
                {
                    "error": type(exc).__name__,
                    "detail": str(exc),
                    "released": False,
                },
                separators=(",", ":"),
            )
        if lease is None:
            return json.dumps(
                {
                    "released": False,
                    "next": "no_open_lease",
                },
                separators=(",", ":"),
            )
        return json.dumps(
            {
                "state": lease["state"],
                "released": True,
                "next": "request_host_access_for_new_scope",
            },
            separators=(",", ":"),
        )


class RequestHostAccessTool(Tool):
    """Submit one immutable, bounded lease application."""

    @property
    def name(self) -> str:
        return "request_host_access"

    @property
    def description(self) -> str:
        return (
            "Request a short, independently reviewed host-access lease when "
            "protected execution cannot complete the user's authorized task. "
            "Describe the concrete purpose, ordered plan, and required time. "
            "An allowed review either activates the lease immediately or sends "
            "it for one user decision, according to the user's setting. "
            "Returns JSON with state, access_granted, and next (one of "
            "lease_active, user_decision, application_rejected, "
            "application_review_failed, lease_activation_failed, "
            "lease_expired_re_request, request_host_access_for_new_scope, "
            "project_policy_changed_re_request_host_access, "
            "lease_state_changed). A rejected application carries a fixed reason "
            "code, safe reason_category, and concrete guidance; its private "
            "reviewer text is withheld. Do not retry an unchanged request. "
            "A blocked review failure reports its known stage without guessing; "
            "reviewer_unavailable adds guidance on when to retry. "
            "For an external-agent exception, submit only the exact literal "
            "command and canonical existing absolute working_directory; approval "
            "always awaits a human decision. If either changes, release_host_access({}) "
            "for this thread's own request, then submit a new application and await "
            "fresh approval. An application belongs to the thread that files it, so a "
            "delegated child may file its own; while it is awaiting_user that child "
            "parks on its parent rather than waiting on its own lease, and it must "
            "never release a request it inherited from a parent. The root receives only "
            "best-effort host_access.activated and may use one bounded fallback to read "
            "authoritative state. A timeout never grants approval or permits execution."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        optional = {
            "type": "string",
            "maxLength": MAX_RISK_FIELD_CHARS,
            "description": "Concise risk context when relevant.",
        }
        return {
            "type": "object",
            "required": ["purpose", "plan", "duration_seconds"],
            "additionalProperties": False,
            "properties": {
                "purpose": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_PURPOSE_CHARS,
                    "description": "The concrete user-authorized outcome.",
                },
                "plan": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_PLAN_STEPS,
                    "items": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_PLAN_STEP_CHARS,
                    },
                    "description": "Ordered actions expected during the lease.",
                },
                "duration_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_LEASE_DURATION_SECONDS,
                    "description": (
                        "Requested lease duration in seconds "
                        "(maximum 30 minutes)."
                    ),
                },
                "expected_resources": {
                    **optional,
                    "description": "Host resources expected to be accessed.",
                },
                "persistent_effects": {
                    **optional,
                    "description": "Effects intended to remain after the lease.",
                },
                "rollback_plan": {
                    **optional,
                    "description": "How persistent effects will be reversed.",
                },
                "forbidden_effects": {
                    **optional,
                    "description": "Effects that must not occur.",
                },
                "requested_policy_exceptions": {
                    "type": "array",
                    "maxItems": MAX_POLICY_EXCEPTIONS,
                    "items": policy_exception_schema(),
                    "description": (
                        "A repository override or an immutable external-agent launch. "
                        "An external launch requires exact command and existing absolute "
                        "working_directory, never repo_name; approval always awaits the human."
                    ),
                },
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        thread_id = kwargs.pop("_thread_id", None)
        kwargs.pop("_thread_project_id", None)
        kwargs.pop("_host_access_turn_identity", None)
        if not thread_id:
            return json.dumps(
                {
                    "error": "host_access_request_unattributed",
                    "access_granted": False,
                },
                separators=(",", ":"),
            )
        try:
            lease = await request_reviewed_lease(thread_id, **kwargs)
        except LeaseError as exc:
            return json.dumps(
                {
                    "error": type(exc).__name__,
                    "detail": str(exc),
                    "access_granted": False,
                },
                separators=(",", ":"),
            )
        next_step = {
            "active": "lease_active",
            "awaiting_user": "user_decision",
            "rejected": "application_rejected",
            "blocked": "application_review_failed",
            "expired": "lease_expired_re_request",
            "revoked": "request_host_access_for_new_scope",
        }.get(lease["state"], "lease_state_changed")
        terminal_reason = lease.get("terminal_reason")
        if terminal_reason == "project_policy_changed":
            next_step = "project_policy_changed_re_request_host_access"
        elif lease["state"] == "blocked" and terminal_reason in {
            ACTIVATION_FAILED_STAGE,
            BROWSER_ISOLATION_FAILED_STAGE,
        }:
            # A post-verdict activation/isolation failure needs a different
            # correction from a review failure, so `next` must not contradict
            # `reason`. Blocked keeps application_review_failed by default, so
            # a block recorded before this change or with a NULL reason still
            # gets a specific next step.
            next_step = "lease_activation_failed"
        payload: dict[str, Any] = {
            "state": lease["state"],
            "access_granted": lease["state"] == "active",
            "next": next_step,
        }
        if lease["state"] == "rejected" and terminal_reason == "application_review_rejected":
            review = lease.get("application_review_result")
            category, guidance = safe_rejection(
                review.get("reason_category") if isinstance(review, dict) else None
            )
            payload["reason"] = terminal_reason
            payload["reason_category"] = category
            payload["guidance"] = guidance
        elif terminal_reason and is_holder_safe(terminal_reason):
            # Only bounded, trusted producer codes are forwarded. Reviewer
            # free text stays in the durable private record.
            payload["reason"] = terminal_reason
            if terminal_reason == REVIEWER_UNAVAILABLE_REASON:
                payload["guidance"] = application_guidance(
                    lease.get(REVIEWER_UNAVAILABLE_ERROR_KEY)
                )
            else:
                cause_guidance = CAUSE_GUIDANCE.get(terminal_reason.partition(": ")[2])
                if cause_guidance is not None:
                    payload["guidance"] = cause_guidance
        return json.dumps(payload, separators=(",", ":"))
