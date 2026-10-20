"""Microsoft identity platform and Graph calls for sign-in, with no provider text leaks."""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from connections.microsoft.config import CONSUMER_TENANT_ID
from connections.microsoft.errors import PROVIDER_UNAVAILABLE, MicrosoftError

TIMEOUT_SECONDS = 20.0
GRAPH_ME_URL = "https://graph.microsoft.com/v1.0/me?$select=id,mail,userPrincipalName"
GRAPH_ORGANIZATION_URL = "https://graph.microsoft.com/v1.0/organization?$select=id"


@dataclass(frozen=True)
class Identity:
    """Who signed in: the row is keyed by tenant plus object id; the address is display only."""

    kind: str
    tenant_id: str
    object_id: str
    address: str


def authority_url(tenant: str) -> str:
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"


async def _graph(http: httpx.AsyncClient, url: str, access_token: str) -> httpx.Response:
    try:
        return await http.get(url, headers={"Authorization": f"Bearer {access_token}"})
    except httpx.HTTPError:
        raise MicrosoftError(PROVIDER_UNAVAILABLE) from None


def _body(response: httpx.Response) -> dict | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


async def _organization_tenant(http: httpx.AsyncClient, access_token: str) -> str | None:
    """The work tenant behind a ``both`` sign-in, or None for a personal account, which has no organization."""
    response = await _graph(http, GRAPH_ORGANIZATION_URL, access_token)
    if response.status_code >= 500:
        raise MicrosoftError(PROVIDER_UNAVAILABLE)
    body = _body(response) if response.status_code == 200 else None
    organizations = body.get("value") if body is not None else None
    if not isinstance(organizations, list) or not organizations:
        return None
    tenant = organizations[0].get("id") if isinstance(organizations[0], dict) else None
    if not isinstance(tenant, str) or not tenant:
        raise MicrosoftError(PROVIDER_UNAVAILABLE)
    return tenant


async def identity(access_token: str, *, kind: str, tenant_id: str | None) -> Identity:
    """Read the signed-in account from Graph ``/me`` with the access token, which stays opaque.

    The tenant comes from the registration the sign-in used: ``consumers`` is the personal
    tenant, ``work`` names its tenant, and ``both`` (``common``) asks Graph for the
    account's organization.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
        response = await _graph(http, GRAPH_ME_URL, access_token)
        body = _body(response) if response.status_code == 200 else None
        object_id = body.get("id") if body is not None else None
        address = (body.get("mail") or body.get("userPrincipalName")) if body is not None else None
        if not isinstance(object_id, str) or not object_id or not isinstance(address, str) or not address.strip():
            raise MicrosoftError(PROVIDER_UNAVAILABLE)
        if kind == "both":
            tenant_id = await _organization_tenant(http, access_token)
    if kind == "personal" or tenant_id is None:
        return Identity("personal", CONSUMER_TENANT_ID, object_id, address.strip())
    return Identity("work", tenant_id.lower(), object_id, address.strip())
