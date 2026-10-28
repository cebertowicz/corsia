"""Owning-thread event router."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Any

from loguru import logger

from events import dead_letter, delivery, guard
from events.identity import (
    CHECK_SUITE_COMPLETED_EVENTS,
    PrResolutionError,
    check_suite_pr_metadata_from_event,
    resolve_identity,
)
from events.resolve import resolve_targets
from events.connectors import github_pr_notice
from events.connectors.github import (
    github_event_class as _github_event_class,
    github_routing_envelope,
    identity_from_check_pr_metadata,
)
from events.github_mapper import pr_terminal_state
from events.routing_inputs import structured_tag
from events.connectors.github_route_preparation import (
    prepare_route_ordered,
    settle_routed_delivery,
)
from infra.product_events import emit_product_event
from infra.worktree_reaper_pr import schedule_merged_pr_reap
from registry import models as registry_models
from registry.pr_lifecycle import record_pull_request_ended

_CHECK_EVENTS = CHECK_SUITE_COMPLETED_EVENTS

_TERMINAL_FAILURE_CONCLUSIONS = frozenset({
    "failure", "timed_out", "action_required",
})


class RouteOutcome(str, Enum):
    """The terminal disposition recorded by the event router."""

    ACCEPTED = "accepted"
    SUPPRESSED = "suppressed"
    DEAD_LETTERED = "dead_lettered"
    DELIVERY_FAILED = "delivery_failed"
    PARKED = "parked"


@dataclass
class RouteResult:
    """Outcome of routing one event."""

    outcome: RouteOutcome
    thread_id: str | None = None
    external_id: str | None = None
    reason: str | None = None


def _dead_letter_result(
    internal_event: str,
    payload: dict[str, Any],
    reason: str,
    external_id: str | None,
) -> RouteResult:
    dl_id = dead_letter.park(internal_event, payload, reason, external_id)
    emit_product_event(
        "event_route", "dead_lettered",
        properties={
            "internal_event": internal_event,
            "external_id": external_id,
            "reason": reason,
            "dead_letter_id": dl_id,
        },
    )
    return RouteResult(
        RouteOutcome.DEAD_LETTERED, external_id=external_id, reason=reason
    )


def _record_passive_change(
    *,
    routing_state: dict[str, Any],
    wake_thread_id: str,
    internal_event: str,
    external_id: str | None,
    summary: str,
    chip: str,
    wake_reason: str,
    tag_provenance: dict[str, str] | None,
) -> None:
    identity = routing_state.get("identity")
    if not identity:
        return
    provenance: dict[str, Any] = {
        "wake_thread_id": wake_thread_id,
        "wake_reason": wake_reason,
    }
    if tag_provenance:
        provenance["tag_provenance"] = tag_provenance
    registry_models.record_artifact_change(
        artifact_id=identity["id"],
        internal_event=internal_event,
        external_id=external_id,
        summary=summary,
        chip=chip,
        provenance=provenance,
        actor_thread_id=wake_thread_id,
    )


def _cancelled_check_result(
    internal_event: str, payload: dict[str, Any],
) -> RouteResult | None:
    if internal_event not in _CHECK_EVENTS:
        return None
    if (payload.get("check_suite") or {}).get("conclusion") != "cancelled":
        return None
    logger.info("event-routing: dropping cancelled check_suite — superseded")
    emit_product_event(
        "event_route", RouteOutcome.PARKED.value,
        properties={"internal_event": internal_event, "external_id": None,
                    "reason": "check_suite_cancelled_dropped"},
    )
    return RouteResult(
        RouteOutcome.PARKED, external_id=None,
        reason="check_suite_cancelled_dropped",
    )


async def _resolve_event_identity(
    internal_event: str, payload: dict[str, Any],
) -> tuple[Any, dict[str, Any] | None]:
    """The event's identity, and for a check suite the pull request it selected."""
    try:
        if internal_event not in CHECK_SUITE_COMPLETED_EVENTS:
            return resolve_identity(internal_event, payload), None
        selected_pr = await asyncio.to_thread(
            check_suite_pr_metadata_from_event, payload,
        )
        return identity_from_check_pr_metadata(payload, selected_pr), selected_pr
    except PrResolutionError as exc:
        logger.warning("event-routing: PR resolution failed for {}: {}", internal_event, exc)
        # The ordered DB stage owns error parking as well as normal route writes.
        return exc, None


def _self_authored_result(
    internal_event: str, payload: dict[str, Any],
) -> RouteResult | None:
    if not guard.is_bot_or_self_authored(payload):
        return None
    comment = payload.get("comment", {})
    comment_user = comment.get("user") if isinstance(comment, dict) else None
    author = comment_user if comment_user is not None else payload.get("sender", {})
    logger.info(
        "event-routing: suppressing {} — authored by bot/self ({})",
        internal_event, author.get("login", "?"),
    )
    return _suppressed_result(internal_event, "self_authored", author=author.get("login"))


def _suppressed_result(internal_event: str, reason: str, *, thread_id: str | None = None,
                       external_id: str | None = None, author: str | None = None) -> RouteResult:
    emit_product_event("event_route", "suppressed", properties={
        "internal_event": internal_event, "reason": reason, "thread_id": thread_id,
        "external_id": external_id, "author": author,
    })
    return RouteResult(RouteOutcome.SUPPRESSED, thread_id=thread_id, external_id=external_id, reason=reason)


def _unowned_check_result(
    internal_event: str,
    payload: dict[str, Any],
    external_id: str | None,
    selected_pr: dict[str, Any] | None,
) -> RouteResult:
    base_ref = selected_pr.get("base_ref") if selected_pr else None
    check_suite = payload.get("check_suite") or {}
    conclusion = check_suite.get("conclusion") or ""
    suite_branch = check_suite.get("head_branch")
    main_failure = conclusion in _TERMINAL_FAILURE_CONCLUSIONS and (
        base_ref == "main"
        or (not base_ref and selected_pr is None and suite_branch == "main")
    )
    if main_failure:
        logger.info(
            "event-routing: unregistered main failure has no owner — "
            "{} dead-lettered (conclusion={}, base_ref={})",
            external_id, conclusion, base_ref or "<unknown>",
        )
        return _dead_letter_result(internal_event, payload, "no_owner", external_id)
    logger.info(
        "event-routing: parking check noise {} — no registered PR owner",
        internal_event,
    )
    emit_product_event(
        "event_route", RouteOutcome.PARKED.value,
        properties={"internal_event": internal_event, "external_id": external_id},
    )
    dead_letter.park(internal_event, payload, "check_suite_noise_parked", external_id)
    return RouteResult(
        RouteOutcome.PARKED, external_id=external_id,
        reason="check_suite_noise_suppressed",
    )


def _select_route(
    internal_event: str,
    payload: dict[str, Any],
    identity,
    external_id: str | None,
    tag: dict[str, str] | None,
    selected_pr: dict[str, Any] | None,
) -> tuple[tuple[str, str] | RouteResult, dict[str, Any]]:
    """The routed thread and why, or the result when there is none, with the
    routing state resolved on the way (agent-written rules read it)."""
    routing_state: dict[str, Any] = {
        "identity": None, "owner": None, "subscribers": [], "triage": None,
    }
    if tag is not None and identity is not None:
        routing_state = registry_models.routing_registrations(*identity)
        thread_id = tag["thread_id"]
        if not delivery.thread_exists_and_active(thread_id):
            logger.warning(
                "event-routing: structured tag target {} inactive/missing for {}",
                thread_id, internal_event,
            )
            return _dead_letter_result(
                internal_event, payload, "invalid_tag_target", external_id,
            ), routing_state
        return (thread_id, "tag"), routing_state

    target = resolve_targets(
        github_routing_envelope(internal_event, identity, payload),
    )[0]
    routing_state = target.routing_state or routing_state
    if target.thread is not None:
        reason = "triage" if target.relationship == "triage" else "owner"
        return (str(target.thread), reason), routing_state
    if target.reason == "owner_inactive":
        return _dead_letter_result(
            internal_event, payload, "owner_inactive", external_id,
        ), routing_state
    if (internal_event in _CHECK_EVENTS
            and routing_state.get("owner") is None
            and routing_state.get("triage") is None):
        return _unowned_check_result(
            internal_event, payload, external_id, selected_pr), routing_state
    reason = "no_artifact" if identity is None else (target.reason or "no_owner")
    return _dead_letter_result(internal_event, payload, reason, external_id), routing_state


async def route_event(
    internal_event: str,
    payload: dict[str, Any],
    *,
    delivery_id: str | None = None,
) -> RouteResult:
    """Resolve an event's owning thread and deliver it as a thread input."""
    cancelled = _cancelled_check_result(internal_event, payload)
    if cancelled is not None:
        if payload.get("action") == "completed" and payload.get("check_suite"):
            from events.connectors.github_head_checks import observe_admitted_check_suite
            observe_admitted_check_suite(payload)
        return cancelled
    identity, selected_pr = await _resolve_event_identity(internal_event, payload)
    if internal_event in CHECK_SUITE_COMPLETED_EVENTS:
        from events.connectors.github_head_checks import observe_admitted_check_suite
        observe_admitted_check_suite(payload, selected_pr)
    prepared = await prepare_route_ordered(
        internal_event,
        payload,
        identity,
        selected_pr,
        delivery_id=delivery_id,
    )

    if prepared.result is not None:
        return prepared.result

    assert prepared.thread_id is not None
    assert prepared.summary is not None
    assert prepared.details is not None
    external_id = identity.external_id if identity else None
    delivered = False
    try:
        delivered = await delivery.deliver_and_resolve_to_thread(
            prepared.thread_id,
            prepared.identity,
            internal_event,
            prepared.summary,
            prepared.details,
            delivery_id=delivery_id,
            chip=prepared.chip,
            notice=prepared.notice,
        )
    finally:
        # A refused or raising delivery (StaleContinuation, cancellation) releases
        # the claim so the next event carries the notice; a repeat beats a loss.
        await settle_routed_delivery(prepared, delivered)
    if not delivered:
        emit_product_event(
            "event_route", "delivery_failed",
            properties={"internal_event": internal_event,
                        "thread_id": prepared.thread_id, "external_id": external_id},
        )
        return RouteResult(
            RouteOutcome.DELIVERY_FAILED, thread_id=prepared.thread_id,
            external_id=external_id,
        )
    outcome = RouteOutcome.ACCEPTED
    emit_product_event(
        "event_route", outcome.value,
        properties={"internal_event": internal_event, "thread_id": prepared.thread_id,
                    "external_id": external_id},
    )
    logger.info(
        "event-routing: {} → thread {} ({})",
        internal_event, prepared.thread_id, outcome.value,
    )
    return RouteResult(outcome, thread_id=prepared.thread_id, external_id=external_id)


def _record_source_facts_db(
    internal_event: str,
    identity: Any,
    payload: dict[str, Any],
) -> bool:
    """Persist source facts and report whether the loop must schedule a merged-PR reap."""
    pr_state = pr_terminal_state(internal_event, payload, identity.external_id)
    if pr_state is None:
        return False
    # The registry owns the terminal state, pane retirement, and nudge in one write.
    record_pull_request_ended(
        identity.integration,
        identity.type,
        identity.external_id,
        pr_state=pr_state,
    )
    return pr_state == "merged"


def _record_source_facts(
    internal_event: str,
    identity: Any,
    payload: dict[str, Any],
) -> None:
    """Persist source facts and schedule a merged-PR reap on the current loop."""
    if _record_source_facts_db(internal_event, identity, payload):
        schedule_merged_pr_reap(payload)


def _summarize(internal_event: str, payload: dict[str, Any], external_id: str | None) -> tuple[str, dict[str, Any]]:
    """Build a one-line summary and structured details for the thread input."""
    repo = (payload.get("repository") or {}).get("full_name", "")
    pr = payload.get("pull_request") or {}
    comment = payload.get("comment") or {}
    details: dict[str, Any] = {"repo": repo, "external_id": external_id}

    if comment:
        author = (comment.get("user") or {}).get("login", "?")
        body = (comment.get("body") or "")[:280]
        details.update({"author": author, "comment": body,
                        "url": comment.get("html_url")})
        return f"{external_id} comment from {author}: {body[:100]}", details

    if pr:
        details.update({"pr_title": pr.get("title"),
                        "head_sha": (pr.get("head") or {}).get("sha"),
                        "base_ref": (pr.get("base") or {}).get("ref"),
                        "url": pr.get("html_url")})
        action = payload.get("action", "")
        return f"{external_id} {action}: {pr.get('title', '')}", details

    check_suite = payload.get("check_suite") or {}
    if check_suite:
        conclusion = check_suite.get("conclusion") or "completed"
        details.update({
            "check_suite_id": check_suite.get("id"),
            "head_sha": check_suite.get("head_sha"),
            "check_suite_updated_at": check_suite.get("updated_at"),
        })
        return f"{internal_event} on {external_id or repo} ({conclusion})", details

    return f"{internal_event} on {external_id or repo}", details


def _chip_line(payload: dict[str, Any], external_id: str | None, summary: str) -> str:
    """The chip's line for a routed event (``summary`` is the agent's): only the
    check-suite summary reads as machine text, the rest are already sentences."""
    suite = payload.get("check_suite") or {}
    if suite and not (payload.get("comment") or payload.get("pull_request")):
        target = external_id or (payload.get("repository") or {}).get("full_name", "")
        return f"CI {suite.get('conclusion') or 'completed'} on {target}"
    return summary
