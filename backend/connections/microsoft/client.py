"""Microsoft identity platform and Graph calls for sign-in, with no provider text leaks."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from connections.microsoft.config import CONSUMER_TENANT_ID
from connections.microsoft.errors import PROVIDER_UNAVAILABLE, REFRESH_TOKEN_INVALID, MicrosoftError, token_error_cause
from infra.db_async import run_in_db

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


def _is_personal_organization_response(response: httpx.Response) -> bool:
    """The observed Graph response for the ``/organization`` API unsupported by personal accounts."""
    body = _body(response)
    error = body.get("error") if body is not None else None
    code = error.get("code") if isinstance(error, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    return (
        response.status_code == 400
        and code == "BadRequest"
        and isinstance(message, str)
        and "not supported for MSA accounts" in message
    )


async def _organization_tenant(http: httpx.AsyncClient, access_token: str) -> str | None:
    """The work tenant behind a ``both`` sign-in, or None for Graph's personal-account response only."""
    response = await _graph(http, GRAPH_ORGANIZATION_URL, access_token)
    if _is_personal_organization_response(response):
        return None
    if response.status_code != 200:
        raise MicrosoftError(PROVIDER_UNAVAILABLE)
    body = _body(response)
    organizations = body.get("value") if body is not None else None
    if not isinstance(organizations, list) or not organizations:
        raise MicrosoftError(PROVIDER_UNAVAILABLE)
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


class DeltaExpired(MicrosoftError):
    """Graph no longer retains this mailbox's delta cursor."""

    def __init__(self) -> None:
        super().__init__(PROVIDER_UNAVAILABLE)


def _on_account(work, account_id: str, *args: object):  # type: ignore[no-untyped-def]
    from infra.database import get_db

    db = get_db()
    try:
        return work(account_id, *args, db=db)
    finally:
        db.close()


def _usable_credentials(account_id: str, *, db) -> dict[str, Any] | None:  # type: ignore[no-untyped-def]
    from connections.connected_accounts import is_connected, resolve_credentials

    return resolve_credentials(account_id, db=db) if is_connected(account_id, db=db) else None


def _store_refreshed(account_id: str, held: str, updated: dict[str, Any], *, db) -> bool:  # type: ignore[no-untyped-def]
    """A refresh cannot restore a signed-out row or overwrite a newer sign-in."""
    import secrets

    from connections.connected_accounts import store_credentials
    from infra.database import write_transaction

    with write_transaction(db, scope="microsoft_refresh") as conn:
        current = _usable_credentials(account_id, db=conn)
        refresh = current.get("refresh_token") if current is not None else None
        if not isinstance(refresh, str) or not secrets.compare_digest(refresh, held):
            return False
        store_credentials(account_id, updated, db=conn)
        return True


async def access_token(account_id: str) -> str:
    """Refresh the connected row using its recorded authority, retaining rotated credentials."""
    from connections.account_state import mark_needs_sign_in

    stored = await run_in_db(_on_account, _usable_credentials, account_id)
    if stored is None:
        raise MicrosoftError(REFRESH_TOKEN_INVALID)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
            response = await http.post(authority_url(stored["tenant_id"]), data={
                "grant_type": "refresh_token", "client_id": stored["client_id"],
                "refresh_token": stored["refresh_token"], "scope": " ".join(stored["scopes"]),
            })
    except httpx.HTTPError:
        raise MicrosoftError(PROVIDER_UNAVAILABLE) from None
    body = _body(response)
    if response.status_code != 200:
        cause = token_error_cause(response.status_code, body)
        if cause == REFRESH_TOKEN_INVALID:
            await run_in_db(_on_account, mark_needs_sign_in, account_id, stored["refresh_token"])
        raise MicrosoftError(cause)
    token = body.get("access_token") if body is not None else None
    if not isinstance(token, str) or not token:
        raise MicrosoftError(PROVIDER_UNAVAILABLE)
    replacement = body.get("refresh_token") if body is not None else None
    updated = {**stored, "access_token": token}
    if isinstance(replacement, str) and replacement:
        updated["refresh_token"] = replacement
    if not await run_in_db(_on_account, _store_refreshed, account_id, stored["refresh_token"], updated):
        raise MicrosoftError(REFRESH_TOKEN_INVALID)
    return token


async def _check_graph(response: httpx.Response, account_id: str) -> None:
    if response.status_code == 401:
        from connections.account_state import mark_needs_sign_in

        await run_in_db(_on_account, mark_needs_sign_in, account_id)
        raise MicrosoftError(REFRESH_TOKEN_INVALID)
    if response.status_code == 410:
        raise DeltaExpired()
    if response.status_code >= 400:
        raise MicrosoftError(PROVIDER_UNAVAILABLE)


async def graph_json(
    method: str, url: str, *, account_id: str, params: dict[str, Any] | None = None,
    body: dict[str, object] | None = None,
) -> Any:
    """One Graph JSON call with a bounded throttle retry and fixed error causes."""
    bearer = await access_token(account_id)
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
                response = await http.request(
                    method, url, headers={"Authorization": f"Bearer {bearer}"}, params=params, json=body,
                )
        except httpx.HTTPError:
            raise MicrosoftError(PROVIDER_UNAVAILABLE) from None
        if response.status_code == 429 and attempt == 0:
            try:
                delay = max(0, min(int(response.headers.get("Retry-After", "1")), 60))
            except ValueError:
                delay = 0
            await asyncio.sleep(delay)
            continue
        await _check_graph(response, account_id)
        if response.status_code in (202, 204):
            return None
        try:
            return response.json()
        except ValueError:
            raise MicrosoftError(PROVIDER_UNAVAILABLE) from None
    raise MicrosoftError(PROVIDER_UNAVAILABLE)


async def graph_bytes(method: str, url: str, *, account_id: str, content: bytes) -> Any:
    """Upload opaque bytes through Graph without logging their contents."""
    bearer = await access_token(account_id)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
            response = await http.request(
                method, url, headers={"Authorization": f"Bearer {bearer}", "Content-Type": "text/plain; charset=utf-8"},
                content=content,
            )
    except httpx.HTTPError:
        raise MicrosoftError(PROVIDER_UNAVAILABLE) from None
    await _check_graph(response, account_id)
    try:
        return response.json()
    except ValueError:
        raise MicrosoftError(PROVIDER_UNAVAILABLE) from None


async def graph_pages(url: str, *, account_id: str, params: dict[str, object] | None = None) -> list[dict[str, object]]:
    """Collect Graph pages, refusing malformed provider bodies."""
    items: list[dict[str, object]] = []
    next_url: str | None = url
    while next_url:
        page = await graph_json("GET", next_url, account_id=account_id, params=params)
        if not isinstance(page, dict):
            raise MicrosoftError(PROVIDER_UNAVAILABLE)
        value = page.get("value")
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise MicrosoftError(PROVIDER_UNAVAILABLE)
        items.extend(value)
        candidate = page.get("@odata.nextLink")
        next_url = candidate if isinstance(candidate, str) and candidate else None
        params = None
    return items


async def graph_delta(
    url: str, *, account_id: str, params: dict[str, object] | None = None,
) -> tuple[list[dict[str, object]], str]:
    """Traverse a delta round completely; expose only its terminal durable link."""
    items: list[dict[str, object]] = []
    while True:
        page = await graph_json("GET", url, account_id=account_id, params=params)
        if not isinstance(page, dict) or not isinstance(page.get("value"), list):
            raise MicrosoftError(PROVIDER_UNAVAILABLE)
        if any(not isinstance(item, dict) for item in page["value"]):
            raise MicrosoftError(PROVIDER_UNAVAILABLE)
        items.extend(page["value"])
        next_link = page.get("@odata.nextLink")
        if isinstance(next_link, str) and next_link:
            url, params = next_link, None
            continue
        delta_link = page.get("@odata.deltaLink")
        if not isinstance(delta_link, str) or not delta_link:
            raise MicrosoftError(PROVIDER_UNAVAILABLE)
        return items, delta_link
