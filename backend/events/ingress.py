"""Webhook event ingress for owner routing and reviewer gates."""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from events.reviewer_route import (
    GATE_TRIGGER_EVENTS,
    REVIEWER_EVENTS,
    check_gate_on_trigger,
    run_review,
)
from events.router import RouteResult, route_event

from events.markers import (
    REVIEW_FP_MARKER_PREFIX as _FP_MARKER_PREFIX,
    REVIEW_RESOLVED_MARKER as _RESOLVED_REPLY_MARKER,
    REVIEW_SUMMARY_MARKER as _SUMMARY_MARKER,
)

_CORSIA_SELF_MARKERS: tuple[str, ...] = (
    _SUMMARY_MARKER,
    _RESOLVED_REPLY_MARKER,
    _FP_MARKER_PREFIX,
)

_COMMENT_EVENT_TYPES: frozenset[str] = frozenset({
    "webhook.github.pr_comment",
    "webhook.github.issue_comment",
})


def _is_corsia_self_comment(internal_event: str, payload: dict[str, Any]) -> bool:
    """True when the event is a comment posted by CORSIA's own reviewer.

    The reviewer posts via the gh/human account, bypassing the bot-identity
    guard. Detect by the HTML marker embedded in every reviewer comment body.
    Returns False for non-comment events (fast path).
    """
    if internal_event not in _COMMENT_EVENT_TYPES:
        return False
    body: str = (payload.get("comment") or {}).get("body") or ""
    return any(
        f"\n{marker}" in body or body.startswith(marker)
        for marker in _CORSIA_SELF_MARKERS
    )


async def route_leg_required(
    internal_event: str,
    payload: dict[str, Any],
    delivery_id: str | None = None,
) -> RouteResult | None:
    """Deliver one event, raising when durable thread routing does not complete."""
    if _is_corsia_self_comment(internal_event, payload):
        logger.info(
            "event-ingress: suppressing corsia self-comment ({}) — marker in body",
            internal_event,
        )
        return None
    return await route_event(internal_event, payload, delivery_id=delivery_id)


async def route_leg(
    internal_event: str,
    payload: dict[str, Any],
    delivery_id: str | None = None,
) -> RouteResult | None:
    """Async routing leg: deliver the event to its owning thread.

    Runs on the backend event loop. Never raises — isolated from the gate leg.
    """
    try:
        return await route_leg_required(internal_event, payload, delivery_id)
    except Exception as exc:  # noqa: BLE001 — isolate from the gate leg
        logger.error("event-ingress: routing leg failed for {}: {}",
                     internal_event, exc)
        return None


def review_leg(internal_event: str, payload: dict[str, Any]) -> bool:
    """Sync push leg — run OFF the event loop.

    Preserves ingress compatibility for PR push events. The reviewer itself is
    triggered by the CI gate, not by this leg. Returns True when the push event
    was processed (False when the event isn't a reviewer event or processing
    errored). Never raises — isolated from the routing leg.
    """
    if internal_event not in REVIEWER_EVENTS:
        return False
    try:
        run_review(internal_event, payload)
        return True
    except Exception as exc:  # noqa: BLE001 — isolate from the routing leg
        logger.error("event-ingress: push-gate leg failed for {}: {}",
                     internal_event, exc)
        return False


def gate_trigger_leg(
    internal_event: str,
    payload: dict[str, Any],
    loop: asyncio.AbstractEventLoop | None = None,
) -> bool:
    """Run reviewer gate triggers OFF the event loop.

    Evaluates on check-suite completion and when a PR becomes ready. Returns
    True when a supported trigger was processed; never raises outside this leg.
    """
    if internal_event not in GATE_TRIGGER_EVENTS:
        return False
    try:
        return check_gate_on_trigger(internal_event, payload, loop)
    except Exception as exc:  # noqa: BLE001 — isolate from the routing leg
        logger.error("event-ingress: reviewer-gate leg failed for {}: {}",
                     internal_event, exc)
        return False


async def handle_event(
    internal_event: str, payload: dict[str, Any], delivery_id: str | None = None
) -> dict[str, Any]:
    """Route an event + run any gated checks. All-in-one for tests / injection.

    Blocking gate legs are offloaded to worker threads so this coroutine
    doesn't stall its loop. Returns a disposition dict.
    """
    result: dict[str, Any] = {"internal_event": internal_event,
                              "delivery_id": delivery_id}

    if _is_corsia_self_comment(internal_event, payload):
        logger.info(
            "event-ingress: suppressing corsia self-comment ({}) — marker in body",
            internal_event,
        )
        result["route_outcome"] = "suppressed_self_comment"
        result["thread_id"] = None
        return result

    route = await route_leg(internal_event, payload, delivery_id)
    result["route_outcome"] = route.outcome.value if route else "error"
    result["thread_id"] = route.thread_id if route else None

    if internal_event in REVIEWER_EVENTS:
        armed = await asyncio.to_thread(review_leg, internal_event, payload)
        result["reviewer"] = "armed" if armed else "error"
    elif internal_event in GATE_TRIGGER_EVENTS:
        evaluated = await asyncio.to_thread(
            gate_trigger_leg, internal_event, payload, asyncio.get_running_loop(),
        )
        result["reviewer_gate"] = "evaluated" if evaluated else "error"

    return result
