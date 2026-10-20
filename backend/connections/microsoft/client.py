"""Microsoft identity platform and Graph calls for sign-in, with no provider text leaks."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from connections.microsoft.config import CONSUMER_TENANT_ID
from connections.microsoft.errors import (
    PROVIDER_UNAVAILABLE,
    REFRESH_TOKEN_INVALID,
    REQUEST_REFUSED,
    MicrosoftError,
    token_error_cause,
)
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


class _GraphUnauthorized(Exception):
    """A Graph 401 that must refresh once before being classified as a refusal."""


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


def _cached_access_token(credentials: dict[str, Any] | None) -> str | None:
    """Return a saved token only when it remains safe to use for one request."""
    if credentials is None:
        return None
    token = credentials.get("access_token")
    expires_at = credentials.get("access_token_expires_at")
    if not isinstance(expires_at, (str, int, float)):
        return None
    try:
        usable_until = float(expires_at)
    except (TypeError, ValueError):
        return None
    if isinstance(token, str) and token and usable_until > time.time() + 60:
        return token
    return None


def _drop_cached_access_token(account_id: str, held: str, *, db) -> None:  # type: ignore[no-untyped-def]
    """Expire only the rejected token; a concurrent fresh sign-in wins unchanged.

    The token field stays because a stored Microsoft bundle requires it; an expiry
    in the past makes the next ``access_token`` call refresh instead of reuse it.
    """
    from connections.connected_accounts import store_credentials
    from infra.database import write_transaction

    with write_transaction(db, scope="microsoft_graph_401") as conn:
        current = _usable_credentials(account_id, db=conn)
        if current is not None and current.get("access_token") == held:
            store_credentials(account_id, {**current, "access_token_expires_at": "0"}, db=conn)


async def access_token(account_id: str, *, _after_race: bool = False) -> str:
    """Return a reusable token or refresh it without losing another caller's rotation."""
    from connections.account_state import mark_needs_sign_in

    stored = await run_in_db(_on_account, _usable_credentials, account_id)
    if stored is None:
        raise MicrosoftError(REFRESH_TOKEN_INVALID)
    cached = _cached_access_token(stored)
    if cached is not None:
        return cached
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
    expires_in = body.get("expires_in") if body is not None else None
    updated = {
        **stored,
        "access_token": token,
        "access_token_expires_at": str(time.time() + expires_in)
        if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool) and expires_in > 0
        else str(time.time()),
    }
    if isinstance(replacement, str) and replacement:
        updated["refresh_token"] = replacement
    if await run_in_db(_on_account, _store_refreshed, account_id, stored["refresh_token"], updated):
        return token
    current = await run_in_db(_on_account, _usable_credentials, account_id)
    cached = _cached_access_token(current)
    if cached is not None:
        return cached
    if current is None:
        raise MicrosoftError(REFRESH_TOKEN_INVALID)
    if _after_race:
        raise MicrosoftError(PROVIDER_UNAVAILABLE)
    return await access_token(account_id, _after_race=True)


async def _check_graph(response: httpx.Response, account_id: str) -> None:
    if response.status_code == 401:
        raise _GraphUnauthorized()
    if response.status_code == 410:
        raise DeltaExpired()
    if response.status_code in (400, 403, 404):
        raise MicrosoftError(REQUEST_REFUSED, never_sent=True)
    if 400 <= response.status_code < 500:
        raise MicrosoftError(PROVIDER_UNAVAILABLE, never_sent=True)
    if response.status_code >= 500:
        raise MicrosoftError(PROVIDER_UNAVAILABLE)


async def _graph_response(
    method: str, url: str, *, account_id: str, token: str | None, headers: dict[str, str],
    conflict_cause: str | None = None, **request: Any,
) -> httpx.Response:
    """Send one Graph request: one throttle retry, one retry after a rejected token."""
    bearer = token or await access_token(account_id)
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
                response = await http.request(
                    method, url, headers={"Authorization": f"Bearer {bearer}", **headers}, **request,
                )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise MicrosoftError(PROVIDER_UNAVAILABLE, never_sent=True) from None
        except httpx.HTTPError:
            raise MicrosoftError(PROVIDER_UNAVAILABLE) from None
        if response.status_code == 429 and attempt == 0:
            try:
                delay = max(0, min(int(response.headers.get("Retry-After", "1")), 60))
            except ValueError:
                delay = 0
            await asyncio.sleep(delay)
            continue
        if response.status_code == 409 and conflict_cause is not None:
            raise MicrosoftError(conflict_cause)
        try:
            await _check_graph(response, account_id)
        except _GraphUnauthorized:
            if attempt:
                raise MicrosoftError(REQUEST_REFUSED, never_sent=True) from None
            await run_in_db(_on_account, _drop_cached_access_token, account_id, bearer)
            bearer = await access_token(account_id)
            continue
        return response
    raise MicrosoftError(PROVIDER_UNAVAILABLE)


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        raise MicrosoftError(PROVIDER_UNAVAILABLE) from None


async def graph_json(
    method: str, url: str, *, account_id: str, params: dict[str, Any] | None = None,
    body: dict[str, object] | None = None, token: str | None = None,
) -> Any:
    """One Graph JSON call with fixed error causes."""
    response = await _graph_response(
        method, url, account_id=account_id, token=token, headers={}, params=params, json=body,
    )
    return None if response.status_code in (202, 204) else _json(response)


async def graph_bytes(
    method: str, url: str, *, account_id: str, content: bytes, params: dict[str, str] | None = None,
    conflict_cause: str | None = None, token: str | None = None,
) -> Any:
    """Upload opaque bytes through Graph without logging their contents."""
    return _json(await _graph_response(
        method, url, account_id=account_id, token=token, conflict_cause=conflict_cause,
        headers={"Content-Type": "text/plain; charset=utf-8"}, content=content, params=params,
    ))


async def graph_pages(
    url: str, *, account_id: str, params: dict[str, object] | None = None, max_items: int | None = None,
) -> list[dict[str, object]]:
    """Collect no more than the requested Graph items, refusing malformed provider bodies."""
    if max_items is not None and max_items < 1:
        raise MicrosoftError(PROVIDER_UNAVAILABLE)
    items: list[dict[str, object]] = []
    next_url: str | None = url
    while next_url and (max_items is None or len(items) < max_items):
        page = await graph_json("GET", next_url, account_id=account_id, params=params)
        if not isinstance(page, dict):
            raise MicrosoftError(PROVIDER_UNAVAILABLE)
        value = page.get("value")
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise MicrosoftError(PROVIDER_UNAVAILABLE)
        remaining = None if max_items is None else max_items - len(items)
        items.extend(value if remaining is None else value[:remaining])
        candidate = page.get("@odata.nextLink")
        next_url = candidate if isinstance(candidate, str) and candidate else None
        params = None
    return items


DELTA_PAGES_PER_POLL = 4


async def graph_delta(
    url: str, *, account_id: str, params: dict[str, object] | None = None,
) -> tuple[list[dict[str, object]], str]:
    """Read a bounded delta slice; persist nextLink until Graph supplies deltaLink."""
    items: list[dict[str, object]] = []
    for _page in range(DELTA_PAGES_PER_POLL):
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
    return items, url
