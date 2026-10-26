"""The one front door behind every ``/webhooks/`` route (event-rules design §5.3, R31).

The platform owns the route, the body limit, the secret and the signature, none of
it connector code: look the connector up (404), enforce its body limit (413), verify
its signature with its own secret (401), then hand the verified delivery to the
connector's translator. The connector's admission owns everything after that:
redelivery dedupe, recording, and its own failures. A translator never sees the secret.

The route awaits the translator, so a fault answers 500 and GitHub shows the delivery
as failed and redeliverable. A request cancelled mid-admission still finishes it: that
is the connector admission's own cancellation join, not this route's.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from fastapi.responses import JSONResponse

from events.connectors import UnregisteredConnectorError, get_manifest
from events.connectors.receiver import ReceiverDecl, ReceiverDelivery, ReceiverOutcome, verify_signature
from infra.product_events import emit_product_event

log = logging.getLogger(__name__)


def receiver_secret(connector: str) -> str | None:
    """The one reader of a connector's webhook secret, or None when it has none.

    Only GitHub's built-in ``github-webhook`` entry is one: no other connector has a
    secret code owns yet, and an entry an agent adds never becomes one.
    """
    from infra.key_store import GITHUB_WEBHOOK, resolve_builtin

    if connector != "github":
        return None
    resolved = resolve_builtin(GITHUB_WEBHOOK)
    return resolved[0] if resolved else None


def _reject(status: int, error: str, reason: str, connector: str) -> JSONResponse:
    if status == 401:
        emit_product_event(
            "webhook", "signature_invalid", properties={"reason": reason, "connector": connector},
        )
    return JSONResponse(content={"error": error}, status_code=status)


async def _read_body(request: Any, headers: Mapping[str, str], limit: int) -> bytes | None:
    """The body, or None when it is over ``limit`` (stops reading at the limit)."""
    declared = headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        return None
    stream = getattr(request, "stream", None)
    if stream is None:
        body = await request.body()
        return body if len(body) <= limit else None
    chunks: list[bytes] = []
    size = 0
    async for chunk in stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _check_signature(
    decl: ReceiverDecl, connector: str, headers: Mapping[str, str], body: bytes, secret: str | None,
) -> JSONResponse | None:
    if not secret:
        log.error("%s webhook secret not configured — rejecting delivery", connector)
        return _reject(401, "webhook secret not configured", "secret_not_configured", connector)
    signature = headers.get(decl.signature_header, "")
    if not signature:
        log.warning("Missing %s header", decl.signature_header)
        return _reject(401, "missing signature", "missing_signature", connector)
    if not verify_signature(decl, body, signature, secret):
        log.warning("Invalid %s webhook signature", connector)
        return _reject(401, "invalid signature", "invalid_signature", connector)
    return None


async def handle_receiver(connector: str, subpath: str, request: Any) -> JSONResponse | dict[str, Any]:
    """Serve one delivery for ``connector``; the answers are the design's R31 table."""
    connector = connector.lower()
    try:
        decl = get_manifest(connector).receiver
    except UnregisteredConnectorError:
        decl = None
    if decl is None:
        return JSONResponse(content={"error": "unknown webhook"}, status_code=404)

    headers = {key.lower(): value for key, value in request.headers.items()}
    body = await _read_body(request, headers, decl.max_body_bytes)
    if body is None:
        return JSONResponse(content={"error": "body too large"}, status_code=413)

    secret = receiver_secret(connector)
    is_open_probe = not secret and decl.open_probe is not None and decl.open_probe(headers)
    if not is_open_probe:
        rejection = _check_signature(decl, connector, headers, body, secret)
        if rejection is not None:
            return rejection

    headers.pop(decl.signature_header, None)
    delivery = ReceiverDelivery(
        connector=connector, subpath=subpath, body=body, headers=headers,
        delivery_id=headers.get(decl.delivery_id_header, ""),
    )
    outcome = await decl.translator(delivery) or ReceiverOutcome()
    if outcome.status_code == 200:
        return outcome.content
    return JSONResponse(content=outcome.content, status_code=outcome.status_code)
