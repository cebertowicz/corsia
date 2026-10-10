"""GitHub webhook ingress endpoint."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from infra.product_events import emit_product_event

router = APIRouter(tags=["webhooks"])
log = logging.getLogger(__name__)

# GitHub sends ping events for health/setup checks. They may be unsigned before
# a webhook secret is configured, but signed once GitHub knows the secret.
_UNSIGNED_ALLOWED_EVENTS: frozenset[str] = frozenset({"ping"})


def _get_webhook_secret() -> str | None:
    """Read the GitHub webhook secret from DB settings."""
    from infra.settings import get_setting
    webhook_cfg = get_setting("webhook") or {}
    return webhook_cfg.get("github_webhook_secret")


def verify_signature(payload_body: bytes, signature_header: str, secret: str) -> bool:
    """Verify the GitHub HMAC-SHA256 signature."""
    if not signature_header.startswith("sha256="):
        return False
    expected_sig = signature_header[7:]  # strip "sha256=" prefix
    mac = hmac.new(secret.encode(), payload_body, hashlib.sha256)
    return hmac.compare_digest(mac.hexdigest(), expected_sig)


def _check_signature(body: bytes, sig_header: str, secret: str | None) -> JSONResponse | None:
    """Validate HMAC signature."""
    if not secret:
        log.error("GitHub webhook secret not configured — rejecting delivery")
        emit_product_event(
            "webhook", "signature_invalid",
            properties={"reason": "secret_not_configured"},
        )
        return JSONResponse(content={"error": "webhook secret not configured"}, status_code=401)

    if not sig_header:
        log.warning("Missing X-Hub-Signature-256 header")
        emit_product_event(
            "webhook", "signature_invalid",
            properties={"reason": "missing_signature"},
        )
        return JSONResponse(content={"error": "missing signature"}, status_code=401)

    if not verify_signature(body, sig_header, secret):
        log.warning("Invalid webhook signature")
        emit_product_event(
            "webhook", "signature_invalid",
            properties={"reason": "invalid_signature"},
        )
        return JSONResponse(content={"error": "invalid signature"}, status_code=401)

    return None


def _record_delivery_for_admission(
    event_type: str,
    action: str | None,
    delivery_id: str,
    repo: str | None,
    payload: dict[str, Any],
):
    """Linearize cutoff classification, dedup, and the final initial status."""
    from events.connectors.github_admission import record_delivery_for_admission

    return record_delivery_for_admission(event_type, action, delivery_id, repo, payload)


def _handle_dedup_and_log(
    event_type: str, action: str | None, delivery_id: str,
    repo: str | None, payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Compatibility helper for focused tests; live ingress uses the record receipt."""
    record, normalized_id, _held = _record_delivery_for_admission(
        event_type, action, delivery_id, repo, payload,
    )
    if record.created:
        return None
    return {"status": "duplicate", "delivery_id": normalized_id}


@router.post("/webhooks/github")
async def github_webhook(request: Request):
    """Receive a GitHub webhook delivery."""
    body = await request.body()
    event_type = request.headers.get("X-GitHub-Event", "")
    sig_header = request.headers.get("X-Hub-Signature-256", "")
    secret = _get_webhook_secret()

    # Ping events are GitHub health/setup checks. When no webhook secret exists
    # yet the ping arrives unsigned; once a secret exists, unsigned pings are
    # anomalous and must fail like every other unsigned delivery.
    if event_type in _UNSIGNED_ALLOWED_EVENTS:
        if secret:
            # Signature present: verify it so misconfigurations are caught early.
            rejection = _check_signature(body, sig_header, secret)
            if rejection is not None:
                return rejection
        log.info("GitHub %s event received — webhook endpoint healthy", event_type)
        emit_product_event(
            "webhook", "ping",
            properties={"event_type": event_type},
        )
        return {"status": "ok", "event": event_type}

    rejection = _check_signature(body, sig_header, secret)
    if rejection is not None:
        return rejection

    try:
        payload: dict[str, Any] = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        log.error("Failed to parse webhook payload: %s", exc)
        return JSONResponse(
            content={"status": "error", "message": "invalid JSON payload"},
            status_code=400,
        )

    from events.connectors.github_admission import admit_github_delivery_async

    outcome = await admit_github_delivery_async(
        event_type, payload, request.headers.get("X-GitHub-Delivery", ""),
    )
    if outcome.status_code == 200:
        return outcome.content
    return JSONResponse(content=outcome.content, status_code=outcome.status_code)
