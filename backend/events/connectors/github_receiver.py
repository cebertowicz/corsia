"""GitHub's webhook receiver declaration: the first receiver on the platform mechanism."""

from __future__ import annotations

import json
import logging
from typing import Any

from events.connectors.receiver import ReceiverDecl, ReceiverDelivery, ReceiverOutcome
from infra.key_store import GITHUB_WEBHOOK

log = logging.getLogger(__name__)

GITHUB_BODY_LIMIT = 25 * 1024 * 1024


async def translate_github(delivery: ReceiverDelivery) -> ReceiverOutcome:
    """Hand a parsed delivery to GitHub's one admission path."""
    event_type = delivery.headers.get("x-github-event", "")
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
    secret=GITHUB_WEBHOOK,
    signature_header="x-hub-signature-256",
    signature_prefix="sha256=",
    delivery_id_header="x-github-delivery",
    max_body_bytes=GITHUB_BODY_LIMIT,
)
