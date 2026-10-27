"""Webhook ingress: every route under ``/webhooks/`` is a signed receiver.

The platform handler (``api.webhook_receivers``) serves ``/webhooks/<connector>/``
from the connector manifest's receiver declaration. ``/webhooks/github`` and
``/webhooks/github/`` stay explicit routes, with no slash redirect (GitHub does not
follow the 307 that turns its POST into a GET), and keep the existing GitHub secret.
Settings, list and test APIs live under ``/api/``, never here.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from api.webhook_receivers import handle_receiver

router = APIRouter(tags=["webhooks"])


@router.post("/webhooks/github")
async def github_webhook(request: Request):
    """Receive a GitHub webhook delivery."""
    return await handle_receiver("github", request)


@router.post("/webhooks/github/")
async def github_webhook_slash(request: Request):
    """The same receiver at the trailing-slash path; an explicit route, never a redirect."""
    return await handle_receiver("github", request)


@router.post("/webhooks/{connector}")
async def connector_webhook(connector: str, request: Request):
    """Receive a delivery for an installed connector's receiver."""
    return await handle_receiver(connector, request)


@router.post("/webhooks/{connector}/")
async def connector_webhook_slash(connector: str, request: Request):
    """Receive the same delivery without a redirect."""
    return await handle_receiver(connector, request)
