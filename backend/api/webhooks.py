"""Webhook ingress: every route under ``/webhooks/`` is a signed receiver.

The platform handler (``api.webhook_receivers``) serves ``/webhooks/<connector>/``
from the connector manifest's receiver declaration. ``/webhooks/github`` and
``/webhooks/github/`` stay explicit routes, with no slash redirect (GitHub does not
follow the 307 that turns its POST into a GET), and keep the existing GitHub secret.
Settings, list and test APIs live under ``/api/``, never here.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request

from api.webhook_receivers import handle_receiver
from events.connectors.receiver import verify_hmac_sha256

router = APIRouter(tags=["webhooks"])
log = logging.getLogger(__name__)


def _get_webhook_secret() -> str | None:
    """Read the GitHub webhook secret from the key store."""
    from infra.key_store import GITHUB_WEBHOOK, resolve_builtin

    resolved = resolve_builtin(GITHUB_WEBHOOK)
    return resolved[0] if resolved else None


def verify_signature(payload_body: bytes, signature_header: str, secret: str) -> bool:
    """Verify the GitHub HMAC-SHA256 signature."""
    return verify_hmac_sha256(payload_body, signature_header, "sha256=", secret)


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
    return await handle_receiver(
        "github", "", request, secret_lookup=lambda _connector: _get_webhook_secret(),
    )


@router.post("/webhooks/github/")
async def github_webhook_slash(request: Request):
    """The same receiver at the trailing-slash path; an explicit route, never a redirect."""
    return await github_webhook(request)


@router.post("/webhooks/{connector}")
async def connector_webhook(connector: str, request: Request):
    """Receive a delivery for an installed connector's receiver."""
    return await handle_receiver(connector, "", request)


@router.post("/webhooks/{connector}/{subpath:path}")
async def connector_webhook_subpath(connector: str, subpath: str, request: Request):
    """A connector owns its whole ``/webhooks/<connector>/`` subtree."""
    return await handle_receiver(connector, subpath, request)
