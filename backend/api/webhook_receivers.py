"""The one front door behind every ``/webhooks/`` route (event-rules design §5.3, R31).

The platform looks the connector up (404), enforces its declared body limit (413),
and verifies its signature against its declared built-in key (401). The connector's
admission owns what follows:
redelivery dedupe, recording, and its own failures. A translator never sees the secret.

The route awaits the translator, so a fault answers 500 and GitHub shows the delivery
as failed and redeliverable. A request cancelled mid-admission still finishes it: that
is the connector admission's own cancellation join, not this route's.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect

from events.connectors import UnregisteredConnectorError, get_manifest
from events.connectors.receiver import ReceiverDecl, ReceiverDelivery, verify_hmac_sha256
from infra.product_events import emit_product_event

log = logging.getLogger(__name__)

#: A delivery whose body plus handler time passes this gets one line of its own, so the
#: time the body spent arriving can be told apart from this process's own work.
SLOW_DELIVERY_MS = 5_000
#: Sender-controlled header values are logged control-character-free and clipped.
_HEADER_CLIP = 60
_PRODUCT_TOKEN_MAX = 40
_ABSENT = "-"
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_REJECTED_FORMAT = (
    "webhook rejected connector=%s status=%s reason=%s ua=%s gh_event=%s gh_hook_id=%s gh_target=%s "
    "has_sha1_sig=%s declared_length=%s body_bytes=%s"
)


@dataclass
class _BodyStats:
    """What the body read received, written by ``_read_body`` as each chunk arrives."""

    size: int = 0
    chunks: int = 0
    aborted: bool = False


def _header_field(value: str | None) -> str:
    """One sender-controlled header value, safe to log: control characters gone, clipped, ``-`` when absent."""
    cleaned = _CONTROL_CHARS.sub("_", value or "").strip()
    return cleaned[:_HEADER_CLIP] or _ABSENT


def _product_token(user_agent: str | None) -> str:
    """The User-Agent product token only: the text before the first ``/`` or space, capped."""
    token = re.split(r"[/\s]", (user_agent or "").strip(), maxsplit=1)[0]
    return token[:_PRODUCT_TOKEN_MAX] or _ABSENT


def _identity(headers: Mapping[str, str]) -> tuple[str, ...]:
    """Who sent this delivery, from non-secret headers only: never a client IP, delivery id, signature or body."""
    return (
        _header_field(headers.get("user-agent")),
        _header_field(headers.get("x-github-event")),
        _header_field(headers.get("x-github-hook-id")),
        _header_field(headers.get("x-github-hook-installation-target-type")),
        "yes" if headers.get("x-hub-signature") else "no",
        _header_field(headers.get("content-length")),
    )


def receiver_secret(decl: ReceiverDecl) -> str | None:
    """Resolve the code-owned key declared by this receiver."""
    from infra.key_store import resolve_builtin

    resolved = resolve_builtin(decl.secret)
    return resolved[0] if resolved else None


def _reject(
    status: int, error: str, reason: str, connector: str, headers: Mapping[str, str], body_bytes: int,
) -> JSONResponse:
    """Refuse one delivery, with one attributed warning line naming who sent it."""
    if status == 401:
        emit_product_event(
            "webhook", "signature_invalid", properties={"reason": reason, "connector": connector},
        )
    log.warning(_REJECTED_FORMAT, connector, status, reason, *_identity(headers), body_bytes)
    return JSONResponse(content={"error": error}, status_code=status)


async def _read_body(
    connector: str, request: Request, headers: Mapping[str, str], limit: int, stats: _BodyStats,
) -> bytes | None:
    """The body, or None when it is over ``limit`` (stops reading at the limit).

    ``stats`` carries the byte and chunk counts whatever happens. A client that disconnects
    mid-body is logged once and the same exception is re-raised unchanged.
    """
    started = time.perf_counter()
    declared = headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        return None
    body = bytearray()
    try:
        async for chunk in request.stream():
            if not chunk:
                continue  # the stream's empty terminator is not a received chunk
            stats.chunks += 1
            if len(body) + len(chunk) > limit:
                return None
            body += chunk
            stats.size = len(body)
        return bytes(body)
    except ClientDisconnect:
        stats.aborted = True
        log.warning(
            "webhook body aborted connector=%s bytes_so_far=%s declared_length=%s elapsed_ms=%s",
            connector, stats.size, _header_field(declared),
            f"{(time.perf_counter() - started) * 1_000:.0f}",
        )
        raise


def _check_signature(
    decl: ReceiverDecl, connector: str, headers: Mapping[str, str], body: bytes, secret: str | None,
    body_bytes: int,
) -> JSONResponse | None:
    if not secret:
        log.error("%s webhook secret not configured — rejecting delivery", connector)
        return _reject(401, "webhook secret not configured", "secret_not_configured", connector, headers, body_bytes)
    signature = headers.get(decl.signature_header, "")
    if not signature:
        return _reject(401, "missing signature", "missing_signature", connector, headers, body_bytes)
    if not verify_hmac_sha256(body, signature, decl.signature_prefix, secret):
        return _reject(401, "invalid signature", "invalid_signature", connector, headers, body_bytes)
    return None


async def handle_receiver(connector: str, request: Request) -> JSONResponse | dict[str, Any]:
    """Serve one delivery for ``connector``; the answers are the design's R31 table.

    Every exit path is timed, including a translator fault: a slow delivery logs how much of
    it was the body arriving (``body_ms``, which the request timer cannot separate) and how
    much was this process's own work. ``status`` is the code answered, or 500 when the
    delivery produced no response of its own (a translator fault, which the route answers
    500). A body the sender abandoned mid-way is logged by ``_read_body`` instead.
    """
    started = time.perf_counter()
    stats = _BodyStats()
    body_ms = 0.0
    status = 500  # a fault that reaches no response is answered 500 by the route
    try:
        connector = connector.lower()
        try:
            decl = get_manifest(connector).receiver
        except UnregisteredConnectorError:
            decl = None
        if decl is None:
            status = 404
            return JSONResponse(content={"error": "unknown webhook"}, status_code=status)

        headers = {key.lower(): value for key, value in request.headers.items()}
        body_started = time.perf_counter()
        try:
            body = await _read_body(connector, request, headers, decl.max_body_bytes, stats)
        finally:
            body_ms = (time.perf_counter() - body_started) * 1_000
        if body is None:
            status = 413
            return _reject(status, "body too large", "body_too_large", connector, headers, stats.size)

        secret = receiver_secret(decl)
        rejection = _check_signature(decl, connector, headers, body, secret, stats.size)
        if rejection is not None:
            status = rejection.status_code
            return rejection

        headers.pop(decl.signature_header, None)
        delivery = ReceiverDelivery(
            connector=connector, body=body, headers=headers,
            delivery_id=headers.get(decl.delivery_id_header, ""),
        )
        outcome = await decl.translator(delivery)
        status = outcome.status_code
        if outcome.status_code == 200:
            return outcome.content
        return JSONResponse(content=outcome.content, status_code=outcome.status_code)
    finally:
        handler_ms = (time.perf_counter() - started) * 1_000 - body_ms
        if not stats.aborted and body_ms + handler_ms > SLOW_DELIVERY_MS:
            log.warning(
                "slow webhook connector=%s status=%d body_ms=%.0f handler_ms=%.0f body_bytes=%d "
                "chunks=%d declared_length=%s agent=%s",
                connector, status, body_ms, handler_ms, stats.size, stats.chunks,
                _header_field(request.headers.get("content-length")),
                _product_token(request.headers.get("user-agent")),
            )
