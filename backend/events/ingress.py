"""Webhook event ingress for owner routing."""

from __future__ import annotations

from typing import Any

from loguru import logger

from events.router import RouteResult, route_event

from events.markers import has_reviewer_marker

_COMMENT_EVENT_TYPES: frozenset[str] = frozenset({
    "webhook.github.pr_comment",
    "webhook.github.issue_comment",
})


def _is_review_marker_comment(internal_event: str, payload: dict[str, Any]) -> bool:
    """True when the event is a comment carrying a ``corsia-review-*`` marker.

    Such comments are output of the PR review workflow and are not routed, so
    the review cannot wake itself; owners get verdicts through the review
    workflow's own notice. The workflow posts through the gh/human account,
    which the bot-identity guard does not catch, so this filter detects the
    HTML marker in the comment body instead. Returns False for non-comment
    events (fast path).
    """
    if internal_event not in _COMMENT_EVENT_TYPES:
        return False
    body: str = (payload.get("comment") or {}).get("body") or ""
    return has_reviewer_marker(body)


async def route_leg_required(
    internal_event: str,
    payload: dict[str, Any],
    delivery_id: str | None = None,
) -> RouteResult | None:
    """Deliver one event, raising when durable thread routing does not complete."""
    if _is_review_marker_comment(internal_event, payload):
        logger.info(
            "event-ingress: suppressing review-marker comment ({}) — marker in body",
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

    Runs on the backend event loop. Never raises.
    """
    try:
        return await route_leg_required(internal_event, payload, delivery_id)
    except Exception as exc:  # noqa: BLE001 — a routing failure is logged and the delivery moves on
        logger.error("event-ingress: routing leg failed for {}: {}",
                     internal_event, exc)
        return None


async def handle_event(
    internal_event: str, payload: dict[str, Any], delivery_id: str | None = None
) -> dict[str, Any]:
    """Route an event. All-in-one for tests / injection. Returns a disposition dict."""
    result: dict[str, Any] = {"internal_event": internal_event,
                              "delivery_id": delivery_id}

    if _is_review_marker_comment(internal_event, payload):
        logger.info(
            "event-ingress: suppressing review-marker comment ({}) — marker in body",
            internal_event,
        )
        result["route_outcome"] = "suppressed_review_marker"
        result["thread_id"] = None
        return result

    route = await route_leg(internal_event, payload, delivery_id)
    result["route_outcome"] = route.outcome.value if route else "error"
    result["thread_id"] = route.thread_id if route else None

    return result
