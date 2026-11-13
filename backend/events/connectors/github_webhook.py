"""One canonical GitHub webhook mapping and asynchronous routing scheduler."""

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


async def dispatch_github_webhook_for_replay(
    event_type: str,
    payload: dict[str, Any],
    *,
    delivery_id: str,
) -> str | None:
    """Await durable thread routing."""
    internal_event = _map_webhook(event_type, payload)
    if not internal_event:
        return None

    from events.ingress import route_leg_required

    await route_leg_required(internal_event, payload, delivery_id)
    return internal_event


def schedule_github_webhook(
    event_type: str,
    payload: dict[str, Any],
    *,
    delivery_id: str | None = None,
    on_settled: Callable[[], None] | None = None,
) -> str | None:
    """Map and schedule the live webhook routing; return the internal event."""
    internal_event = _map_webhook(event_type, payload)
    if not internal_event:
        return None

    from events.ingress import route_leg

    task = asyncio.create_task(
        route_leg(internal_event, payload, delivery_id),
        name=f"webhook-dispatch:{delivery_id or internal_event}",
    )
    _BG_DISPATCH.add(task)
    task.add_done_callback(
        lambda completed: _settle_dispatch(completed, on_settled=on_settled)
    )
    return internal_event
