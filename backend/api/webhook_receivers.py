"""The one front door behind every ``/webhooks/`` route (event-rules design §5.3, R31).

The platform owns the route, the body limit, the secret and the signature, none of
it connector code: look the connector up (404), enforce its body limit (413), verify
its signature with its own secret (401), then hand the verified delivery to the
connector's translator. The connector's admission owns everything after that:
redelivery dedupe, recording, and its own failures. A translator never sees the secret.

The translator runs in a task that outlives the request. A fault inside 8 s answers
500 as GitHub's admission documents; past 8 s the route answers 202 (GitHub's limit
is 10 s) and a later failure is reported as a product event.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from fastapi.responses import JSONResponse

from events.connectors import UnregisteredConnectorError, get_manifest
from events.connectors.receiver import ReceiverDecl, ReceiverDelivery, ReceiverOutcome, verify_signature
from infra.product_events import emit_product_event

log = logging.getLogger(__name__)

#: GitHub gives a delivery 10 s; past this the route answers 202 and admission carries on.
ADMIT_WAIT_SECONDS = 8.0
_BG_ADMISSION: set[asyncio.Task] = set()


def receiver_secret(connector: str) -> str | None:
    """The one reader of a connector's webhook secret, or None when it has none.

    GitHub's is the built-in ``github-webhook`` entry, which serves the legacy routes
    and ``/webhooks/github/<sub>`` alike. Any other connector's is ``webhook-<name>``,
    a name only package install writes (``infra.key_store.WEBHOOK_PREFIX`` is reserved).
    """
    from infra.key_store import GITHUB_WEBHOOK, resolve_builtin, resolve_webhook

    if connector == "github":
        resolved = resolve_builtin(GITHUB_WEBHOOK)
        return resolved[0] if resolved else None
    return resolve_webhook(connector)


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


def _settled(
    task: asyncio.Task, delivery: ReceiverDelivery, unanswered: list[bool],
) -> None:
    """Report a failure the response could not carry: the 202 went out, or the caller left."""
    _BG_ADMISSION.discard(task)
    if not unanswered:
        return
    if task.cancelled():
        log.warning("%s webhook admission cancelled during shutdown", delivery.connector)
        return
    error = task.exception()
    if error is None:
        return
    # The class name only: an exception's text can carry a value from the delivery.
    log.error(
        "%s webhook admission of delivery %s failed after the 202: %s",
        delivery.connector, delivery.delivery_id, type(error).__name__,
    )
    emit_product_event(
        "webhook", "admission_failed",
        properties={
            "connector": delivery.connector, "delivery_id": delivery.delivery_id,
            "error": type(error).__name__,
        },
    )


async def _admit_within_deadline(translator: Any, delivery: ReceiverDelivery) -> ReceiverOutcome:
    task = asyncio.create_task(translator(delivery), name=f"webhook-admission:{delivery.connector}")
    unanswered: list[bool] = []
    _BG_ADMISSION.add(task)
    task.add_done_callback(lambda done: _settled(done, delivery, unanswered))
    try:
        done, _ = await asyncio.wait({task}, timeout=ADMIT_WAIT_SECONDS)
    except asyncio.CancelledError:
        unanswered.append(True)
        raise
    if task not in done:
        unanswered.append(True)
        log.warning(
            "%s webhook delivery %s still admitting after %ss — answering 202",
            delivery.connector, delivery.delivery_id, ADMIT_WAIT_SECONDS,
        )
        return ReceiverOutcome(202, {"status": "accepted", "admission": "continuing"})
    return task.result() or ReceiverOutcome()


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
    if not (not secret and decl.open_probe is not None and decl.open_probe(headers)):
        rejection = _check_signature(decl, connector, headers, body, secret)
        if rejection is not None:
            return rejection

    headers.pop(decl.signature_header, None)
    delivery = ReceiverDelivery(
        connector=connector, subpath=subpath, body=body, headers=headers,
        delivery_id=headers.get(decl.delivery_id_header, ""),
    )
    outcome = await _admit_within_deadline(decl.translator, delivery)
    if outcome.status_code == 200:
        return outcome.content
    return JSONResponse(content=outcome.content, status_code=outcome.status_code)
