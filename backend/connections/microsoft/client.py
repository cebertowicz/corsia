"""Minimal Microsoft Graph OAuth client for one account, with no provider text leaks."""
from __future__ import annotations

import base64
import json
from typing import Any

import httpx

from connections.microsoft.errors import MicrosoftError, PROVIDER_UNAVAILABLE, REFRESH_TOKEN_INVALID, token_error_cause
from infra.db_async import run_in_db

TIMEOUT_SECONDS = 20.0
GRAPH_ME_URL = "https://graph.microsoft.com/v1.0/me?$select=id,mail,userPrincipalName"


def authority_url(tenant: str) -> str:
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"


def _on_account(work, account_id: str, *args: object):  # type: ignore[no-untyped-def]
    from infra.database import get_db

    db = get_db()
    try:
        return work(account_id, *args, db=db)
    finally:
        db.close()


def id_token_identity(value: object) -> tuple[str, str, str | None] | None:
    """Read tenant, object and display address from an unsigned token payload.

    The token has already been delivered over TLS by the authority selected from
    validated input. It is never exposed to an agent or returned by this helper.
    """
    if not isinstance(value, str):
        return None
    pieces = value.split(".")
    if len(pieces) != 3:
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(pieces[1] + "=" * (-len(pieces[1]) % 4)))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(claims, dict):
        return None
    tenant, object_id = claims.get("tid"), claims.get("oid")
    address = claims.get("preferred_username") or claims.get("email")
    if not isinstance(tenant, str) or not tenant or not isinstance(object_id, str) or not object_id:
        return None
    return tenant, object_id, address if isinstance(address, str) and address else None


async def profile(access_token: str) -> tuple[str, str, str] | None:
    """Read a Microsoft identity from Graph when the ID token lacks an address."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
            response = await http.get(GRAPH_ME_URL, headers={"Authorization": f"Bearer {access_token}"})
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        body = response.json()
        object_id = body.get("id")
        address = body.get("mail") or body.get("userPrincipalName")
    except ValueError:
        return None
    return ("", object_id, address) if isinstance(object_id, str) and object_id and isinstance(address, str) and address else None


async def access_token(account_id: str) -> str:
    """Refresh one Microsoft row and replace its rotated refresh token when supplied."""
    from connections.microsoft.accounts import mark_microsoft_needs_sign_in, resolve_microsoft_credentials, store_microsoft_credentials

    stored = await run_in_db(_on_account, resolve_microsoft_credentials, account_id)
    if stored is None:
        raise MicrosoftError(REFRESH_TOKEN_INVALID)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
            response = await http.post(authority_url(stored["tenant_id"]), data={
                "grant_type": "refresh_token", "client_id": stored["client_id"], "refresh_token": stored["refresh_token"],
                "scope": " ".join(stored["scopes"]),
            })
    except httpx.HTTPError:
        raise MicrosoftError(PROVIDER_UNAVAILABLE) from None
    try:
        body = response.json()
    except ValueError:
        body = None
    if response.status_code != 200:
        cause = token_error_cause(response.status_code, body)
        if cause == REFRESH_TOKEN_INVALID:
            await run_in_db(_on_account, mark_microsoft_needs_sign_in, account_id, stored["refresh_token"])
        raise MicrosoftError(cause)
    token = body.get("access_token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise MicrosoftError(PROVIDER_UNAVAILABLE)
    replacement = body.get("refresh_token") if isinstance(body, dict) else None
    if isinstance(replacement, str) and replacement:
        updated: dict[str, object] = {**stored, "refresh_token": replacement}
        await run_in_db(_on_account, store_microsoft_credentials, account_id, updated)
    return token


async def request_json(method: str, url: str, *, account_id: str | None = None, token: str | None = None) -> Any:
    """One Graph JSON request without leaking provider responses."""
    bearer = token or (await access_token(str(account_id)))
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
            response = await http.request(method, url, headers={"Authorization": f"Bearer {bearer}"})
    except httpx.HTTPError:
        raise MicrosoftError(PROVIDER_UNAVAILABLE) from None
    if response.status_code == 401 and account_id is not None:
        from connections.microsoft.accounts import mark_microsoft_needs_sign_in
        await run_in_db(_on_account, mark_microsoft_needs_sign_in, account_id)
        raise MicrosoftError(REFRESH_TOKEN_INVALID)
    if response.status_code >= 400:
        raise MicrosoftError(PROVIDER_UNAVAILABLE)
    try:
        return response.json()
    except ValueError:
        raise MicrosoftError(PROVIDER_UNAVAILABLE) from None
