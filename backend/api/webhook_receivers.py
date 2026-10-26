"""The one handler behind every ``/webhooks/`` route (event-rules design §5.3, R31).

Order, none of it connector code: look the connector up (404), enforce its body
limit (413), verify its signature with its own secret (401), then admit in a task
that outlives the request. A translator only ever sees a verified delivery.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any

from fastapi.responses import JSONResponse

from events.connectors import UnregisteredConnectorError, get_manifest
from events.connectors.receiver import (
    ReceiverDecl,
    ReceiverDelivery,
    ReceiverOutcome,
    verify_signature,
)
from infra.product_events import emit_product_event

log = logging.getLogger(__name__)

#: GitHub gives a delivery 10 s; past this the route answers 202 and admission carries on.
ADMIT_WAIT_SECONDS = 8.0
#: A locked database is retried this long inside the admission task, then the delivery is dropped.
LOCK_RETRY_SECONDS = 120.0
_LOCK_RETRY_PAUSE = 0.5
_BG_ADMISSION: set[asyncio.Task] = set()

SecretLookup = Callable[[str], str | None]


def receiver_secret(connector: str) -> str | None:
    """The connector's webhook secret from the key store (``<connector>-webhook``), or None."""
    from infra.key_store import resolve_names

    name = f"{connector}-webhook"
    return resolve_names([name]).get(name)


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


def _delivery_seen(delivery_id: str) -> bool:
    from infra.database import get_db

    row = get_db().execute(
        "SELECT 1 FROM webhook_deliveries WHERE delivery_id=?", (delivery_id,),
    ).fetchone()
    return row is not None


def _record_delivery(connector: str, delivery_id: str) -> None:
    from infra.webhook_delivery_log import record_delivery

    record_delivery(
        event_type="receiver", action=connector, delivery_id=delivery_id,
        repo=None, payload={}, status="dispatched",
    )


def _is_locked(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and (
        "locked" in str(exc).lower() or "busy" in str(exc).lower()
    )


async def _with_lock_retry(connector: str, call: Callable[[], Any]) -> Any:
    """Retry a call a held database writer refused; admission is idempotent by event id."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + LOCK_RETRY_SECONDS
    while True:
        try:
            return await call()
        except sqlite3.OperationalError as exc:
            if not _is_locked(exc) or loop.time() >= deadline:
                raise
            log.warning("%s webhook admission waiting on a locked database", connector)
            await asyncio.sleep(_LOCK_RETRY_PAUSE)


async def _admit(decl: ReceiverDecl, delivery: ReceiverDelivery) -> ReceiverOutcome:
    """Drop a redelivery, run the translator, then record the delivery id.

    The id is recorded after admission so a delivery that failed halfway is admitted
    again on redelivery; a second admission of the same happening is ``duplicate`` there.
    """
    from infra.db_async import run_in_db
    from infra.drain import begin_turn_admission, finish_turn_admission

    scoped_id = f"{delivery.connector}:{delivery.delivery_id}" if delivery.delivery_id else ""
    if decl.translator_dedupes:
        return await _with_lock_retry(
            delivery.connector, lambda: decl.translator(delivery),
        ) or ReceiverOutcome()
    admission = begin_turn_admission()
    try:
        if scoped_id and await run_in_db(_delivery_seen, scoped_id):
            log.info("Duplicate %s delivery %s — skipping", delivery.connector, delivery.delivery_id)
            return ReceiverOutcome(200, {"status": "duplicate", "delivery_id": delivery.delivery_id})

        async def _translate() -> ReceiverOutcome | None:
            return await decl.translator(delivery)

        outcome = await _with_lock_retry(delivery.connector, _translate) or ReceiverOutcome()
        if scoped_id:
            await _with_lock_retry(
                delivery.connector, lambda: run_in_db(_record_delivery, delivery.connector, scoped_id),
            )
        return outcome
    finally:
        finish_turn_admission(admission)


def _settled(task: asyncio.Task, connector: str) -> None:
    _BG_ADMISSION.discard(task)
    if task.cancelled():
        log.warning("%s webhook admission cancelled during shutdown", connector)
    elif task.exception() is not None:
        # The class name only: an exception's text can carry a value from the delivery.
        log.error("%s webhook admission failed: %s", connector, type(task.exception()).__name__)


async def _admit_within_deadline(decl: ReceiverDecl, delivery: ReceiverDelivery) -> ReceiverOutcome:
    task = asyncio.create_task(_admit(decl, delivery), name=f"webhook-admission:{delivery.connector}")
    _BG_ADMISSION.add(task)
    task.add_done_callback(lambda done: _settled(done, delivery.connector))
    done, _ = await asyncio.wait({task}, timeout=ADMIT_WAIT_SECONDS)
    if task in done:
        return task.result()
    log.warning("%s webhook admission still running after %ss — answering 202", delivery.connector, ADMIT_WAIT_SECONDS)
    return ReceiverOutcome(202, {"status": "accepted", "admission": "continuing"})


async def handle_receiver(
    connector: str,
    subpath: str,
    request: Any,
    *,
    secret_lookup: SecretLookup | None = None,
) -> JSONResponse | dict[str, Any]:
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

    secret = (secret_lookup or receiver_secret)(connector)
    if not (not secret and decl.open_probe is not None and decl.open_probe(headers)):
        rejection = _check_signature(decl, connector, headers, body, secret)
        if rejection is not None:
            return rejection

    headers.pop(decl.signature_header, None)
    delivery = ReceiverDelivery(
        connector=connector, subpath=subpath, body=body, headers=headers,
        delivery_id=headers.get(decl.delivery_id_header, ""),
    )
    outcome = await _admit_within_deadline(decl, delivery)
    if outcome.status_code == 200:
        return outcome.content
    return JSONResponse(content=outcome.content, status_code=outcome.status_code)
