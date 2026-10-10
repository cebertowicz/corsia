"""One canonical GitHub webhook mapping and asynchronous leg scheduler."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from events.github_mapper import map_github_event

log = logging.getLogger(__name__)
_BG_DISPATCH: set[asyncio.Task] = set()


class WebhookDispatchPoison(ValueError):
    """A durable payload cannot be mapped/scheduled without operator correction."""


def _map_webhook(event_type: str, payload: dict[str, Any]) -> str | None:
    if not isinstance(payload, dict):
        raise WebhookDispatchPoison("webhook payload is not a JSON object")
    try:
        return map_github_event(event_type, payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise WebhookDispatchPoison(str(exc)) from exc


async def _run_webhook_legs(
    internal_event: str,
    payload: dict[str, Any],
    *,
    route_leg,
    review_leg,
    gate_trigger_leg,
    reviewer_events: frozenset[str],
    gate_trigger_events: frozenset[str],
    delivery_id: str | None,
) -> None:
    legs = [route_leg(internal_event, payload, delivery_id)]
    loop = asyncio.get_running_loop()
    if internal_event in reviewer_events:
        legs.append(asyncio.to_thread(review_leg, internal_event, payload))
    elif internal_event in gate_trigger_events:
        legs.append(asyncio.to_thread(gate_trigger_leg, internal_event, payload, loop))
    results = await asyncio.gather(*legs, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            log.error("Webhook leg failed for %s: %s", internal_event, result)


def _settle_dispatch(
    task: asyncio.Task,
    *,
    on_settled: Callable[[], None] | None,
) -> None:
    _BG_DISPATCH.discard(task)
    try:
        task.exception()
    except asyncio.CancelledError:
        log.warning("Webhook dispatch cancelled during shutdown")
    except BaseException as exc:
        log.error("Webhook dispatch failed before leg settlement: %s", exc)
    finally:
        if on_settled is not None:
            on_settled()


async def _run_webhook_auxiliary_leg(
    internal_event: str,
    payload: dict[str, Any],
    *,
    review_leg,
    gate_trigger_leg,
    reviewer_events: frozenset[str],
    gate_trigger_events: frozenset[str],
) -> None:
    loop = asyncio.get_running_loop()
    if internal_event in reviewer_events:
        result = await asyncio.to_thread(review_leg, internal_event, payload)
    elif internal_event in gate_trigger_events:
        result = await asyncio.to_thread(gate_trigger_leg, internal_event, payload, loop)
    else:
        return
    if result is False:
        log.error("Webhook auxiliary leg failed for %s", internal_event)


async def dispatch_github_webhook_for_replay(
    event_type: str,
    payload: dict[str, Any],
    *,
    delivery_id: str,
) -> str | None:
    """Await durable thread routing, then background only reviewer/gate work."""
    internal_event = _map_webhook(event_type, payload)
    if not internal_event:
        return None

    from events.ingress import (
        GATE_TRIGGER_EVENTS,
        REVIEWER_EVENTS,
        gate_trigger_leg,
        review_leg,
        route_leg_required,
    )

    await route_leg_required(internal_event, payload, delivery_id)
    if internal_event not in REVIEWER_EVENTS and internal_event not in GATE_TRIGGER_EVENTS:
        return internal_event

    task = asyncio.create_task(
        _run_webhook_auxiliary_leg(
            internal_event,
            payload,
            review_leg=review_leg,
            gate_trigger_leg=gate_trigger_leg,
            reviewer_events=REVIEWER_EVENTS,
            gate_trigger_events=GATE_TRIGGER_EVENTS,
        ),
        name=f"webhook-auxiliary:{delivery_id}",
    )
    _BG_DISPATCH.add(task)
    task.add_done_callback(
        lambda completed: _settle_dispatch(completed, on_settled=None)
    )
    return internal_event


def schedule_github_webhook(
    event_type: str,
    payload: dict[str, Any],
    *,
    delivery_id: str | None = None,
    on_settled: Callable[[], None] | None = None,
) -> str | None:
    """Map and schedule the exact live webhook legs; return the internal event."""
    internal_event = _map_webhook(event_type, payload)
    if not internal_event:
        return None

    from events.ingress import (
        GATE_TRIGGER_EVENTS,
        REVIEWER_EVENTS,
        gate_trigger_leg,
        review_leg,
        route_leg,
    )

    task = asyncio.create_task(
        _run_webhook_legs(
            internal_event,
            payload,
            route_leg=route_leg,
            review_leg=review_leg,
            gate_trigger_leg=gate_trigger_leg,
            reviewer_events=REVIEWER_EVENTS,
            gate_trigger_events=GATE_TRIGGER_EVENTS,
            delivery_id=delivery_id,
        ),
        name=f"webhook-dispatch:{delivery_id or internal_event}",
    )
    _BG_DISPATCH.add(task)
    task.add_done_callback(
        lambda completed: _settle_dispatch(completed, on_settled=on_settled)
    )
    return internal_event
