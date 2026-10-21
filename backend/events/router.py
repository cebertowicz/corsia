"""Owning-thread event router."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Any

from loguru import logger

from events import dead_letter, delivery, guard
from events.rules import dropping_rule, evaluate
from infra.database import get_db
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
    release_notice_off_loop,
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


def record_drop(envelope: Any, rule_id: str, *, author: str | None) -> None:
    """The one record of a rule drop: a log line and the suppressed product event."""
    logger.info("event-routing: rule {} dropped {} (author {})", rule_id, envelope.type, author or '?')
    emit_product_event(
        "event_route", "suppressed",
        properties={"internal_event": envelope.type, "author": author, "rule_id": rule_id},
    )


def _rule_drop_result(
    internal_event: str, payload: dict[str, Any], identity: Any = None,
) -> RouteResult | None:
    """The webhook's one drop pass: once per event, after source facts, or before
    a check event's identity read. Maps a rule drop to its one result."""
    envelope = github_routing_envelope(internal_event, identity, payload)
    db = get_db()
    try:
        rule_id = dropping_rule(envelope, db=db)
    finally:
        db.close()
    if rule_id is None:
        return None
    record_drop(envelope, rule_id, author=guard._event_author(payload).get('login'))
    return RouteResult(RouteOutcome.SUPPRESSED, reason=rule_id)


async def _resolve_event_identity(
    internal_event: str, payload: dict[str, Any],
) -> tuple[Any, str | None, bool]:
    try:
        if internal_event not in CHECK_SUITE_COMPLETED_EVENTS:
            return resolve_identity(internal_event, payload), None, False
        selected_pr = await asyncio.to_thread(
            check_suite_pr_metadata_from_event, payload,
        )
        return (
            identity_from_check_pr_metadata(payload, selected_pr),
            selected_pr.get("base_ref") if selected_pr else None,
            selected_pr is not None,
        )
    except PrResolutionError as exc:
        logger.warning("event-routing: PR resolution failed for {}: {}", internal_event, exc)
        # The ordered DB stage owns error parking as well as normal route writes.
        return exc, None, False


def _unowned_check_result(
    internal_event: str,
    payload: dict[str, Any],
    external_id: str | None,
    base_ref: str | None,
    pr_selected: bool,
) -> RouteResult:
    check_suite = payload.get("check_suite") or {}
    conclusion = check_suite.get("conclusion") or ""
    suite_branch = check_suite.get("head_branch")
    main_failure = conclusion in _TERMINAL_FAILURE_CONCLUSIONS and (
        base_ref == "main"
        or (not base_ref and not pr_selected and suite_branch == "main")
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
    check_suite_base_ref: str | None,
    check_suite_pr_selected: bool,
) -> tuple[str, str, dict[str, Any]] | RouteResult:
    routing_state: dict[str, Any] = {
        "identity": None, "owner": None, "subscribers": [], "triage": None,
    }
    envelope = github_routing_envelope(internal_event, identity, payload)
    db = get_db()
    try:
        targets = evaluate(envelope, db=db,
                           resolve_owner=lambda: resolve_targets(envelope))
    finally:
        db.close()
    if len(targets) > 1:
        # One-recipient routing never picks among rule recipients.
        logger.error("event-routing: rules chose {} recipients for {}",
                     len(targets), internal_event)
        return _dead_letter_result(internal_event, payload, 'multiple_rule_recipients', external_id)
    if not targets:
        return _dead_letter_result(internal_event, payload, 'no_owner', external_id)
    target = targets[0]
    routing_state = target.routing_state or (
        registry_models.routing_registrations(*identity) if identity else routing_state)
    if target.thread is not None and target.disposition == 'dead_letter':
        return _dead_letter_result(internal_event, payload, str(target.reason), external_id)
    if tag is not None and identity is not None:
        return str(target.thread), 'tag', routing_state
    if target.thread is not None:
        reason = "triage" if target.relationship == "triage" else "owner"
        return str(target.thread), reason, routing_state
    if target.reason == "owner_inactive":
        return _dead_letter_result(
            internal_event, payload, "owner_inactive", external_id,
        )
    if (internal_event in _CHECK_EVENTS
            and routing_state.get("owner") is None
            and routing_state.get("triage") is None):
        return _unowned_check_result(
            internal_event, payload, external_id,
            check_suite_base_ref, check_suite_pr_selected,
        )
    reason = "no_artifact" if identity is None else (target.reason or "no_owner")
    return _dead_letter_result(internal_event, payload, reason, external_id)


async def route_event(
    internal_event: str,
    payload: dict[str, Any],
    *,
    delivery_id: str | None = None,
) -> RouteResult:
    """Resolve an event's owning thread and deliver it as a thread input."""
    if internal_event in _CHECK_EVENTS:
        from infra.db_async import run_in_db

        # Check identity may need a provider read; a dropped event skips it.
        dropped = await run_in_db(_rule_drop_result, internal_event, payload)
        if dropped is not None:
            return dropped
    resolved = await _resolve_event_identity(internal_event, payload)

    identity, check_suite_base_ref, check_suite_pr_selected = resolved
    prepared = await prepare_route_ordered(
        internal_event,
        payload,
        identity,
        check_suite_base_ref,
        check_suite_pr_selected,
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
        if not delivered and prepared.notice:
            await release_notice_off_loop(prepared.thread_id, prepared.identity)
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


def _summarize(
    internal_event: str, payload: dict[str, Any], external_id: str | None
) -> tuple[str, dict[str, Any]]:
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
        details.update(
            {
                "check_suite_id": check_suite.get("id"),
                "head_sha": check_suite.get("head_sha"),
                "check_suite_updated_at": check_suite.get("updated_at"),
            }
        )
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
