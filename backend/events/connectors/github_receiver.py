"""GitHub's webhook receiver declaration: the first receiver on the platform mechanism."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from events.connectors.receiver import ReceiverDecl, ReceiverDelivery, ReceiverOutcome

log = logging.getLogger(__name__)

# GitHub sends ping events for health/setup checks. They may be unsigned before
# a webhook secret is configured, but signed once GitHub knows the secret.
_UNSIGNED_ALLOWED_EVENTS: frozenset[str] = frozenset({"ping"})
GITHUB_BODY_LIMIT = 25 * 1024 * 1024


def _is_ping(headers: Mapping[str, str]) -> bool:
    return headers.get("x-github-event", "") in _UNSIGNED_ALLOWED_EVENTS


async def translate_github(delivery: ReceiverDelivery) -> ReceiverOutcome:
    """Answer a ping, or hand a parsed delivery to GitHub's one admission path."""
    from infra.product_events import emit_product_event

    event_type = delivery.headers.get("x-github-event", "")
    if _is_ping(delivery.headers):
        log.info("GitHub %s event received — webhook endpoint healthy", event_type)
        emit_product_event("webhook", "ping", properties={"event_type": event_type})
        return ReceiverOutcome(200, {"status": "ok", "event": event_type})
    try:
        payload: dict[str, Any] = json.loads(delivery.body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        log.error("Failed to parse webhook payload: %s", exc)
        return ReceiverOutcome(400, {"status": "error", "message": "invalid JSON payload"})

    from events.connectors.github_admission import admit_github_delivery_async

    outcome = await admit_github_delivery_async(event_type, payload, delivery.delivery_id)
    return ReceiverOutcome(outcome.status_code, outcome.content)


GITHUB_RECEIVER = ReceiverDecl(
    translator=translate_github,
    signature_header="x-hub-signature-256",
    signature_prefix="sha256=",
    delivery_id_header="x-github-delivery",
    max_body_bytes=GITHUB_BODY_LIMIT,
    open_probe=_is_ping,
)
