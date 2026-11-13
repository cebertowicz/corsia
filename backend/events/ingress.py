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


def _is_corsia_self_comment(internal_event: str, payload: dict[str, Any]) -> bool:
    """True when the event is a comment posted by CORSIA's own reviewer.

    The reviewer posts via the gh/human account, bypassing the bot-identity
    guard. Detect by the HTML marker embedded in every reviewer comment body.
    Returns False for non-comment events (fast path).
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

    Runs on the backend event loop. Never raises.
    """
    try:
        return await route_leg_required(internal_event, payload, delivery_id)
    except Exception as exc:  # noqa: BLE001 — isolate from the gate leg
        logger.error("event-ingress: routing leg failed for {}: {}",
                     internal_event, exc)
        return None


async def handle_event(
    internal_event: str, payload: dict[str, Any], delivery_id: str | None = None
) -> dict[str, Any]:
    """Route an event. All-in-one for tests / injection. Returns a disposition dict."""
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

    return result
