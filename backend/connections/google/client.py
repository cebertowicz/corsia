"""A minimal authorized Google HTTP client, one connected account per call.

Every error here carries fixed text and never a token, a secret or Google's
response body: callers may hand the message straight to an agent.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import httpx

from infra.db_async import run_in_db

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
STATUS_CONNECTED = "connected"
STATUS_NEEDS_SIGN_IN = "needs_sign_in"
TIMEOUT_SECONDS = 20.0

NOT_CONNECTED = "Google isn't connected. Call google_connect and give the person the link."
SIGN_IN_RECOVERY = (
    'Google rejected the saved sign-in. Sign-ins for a project in Testing expire after 7 days: '
    'if the Audience page of the OAuth consent screen in Google Cloud shows Testing, ask the person '
    'to publish their app "In production", keeping the same project. Then call google_connect with '
    'this account_id and give them the link.'
)
NEEDS_SIGN_IN = SIGN_IN_RECOVERY
UNREACHABLE = "Couldn't reach Google. Try again later."
# Transport errors raised before any request bytes reach Google.
_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class GoogleError(Exception):
    """Base of every error this package raises; its text is safe to show."""


class GoogleNotConnected(GoogleError):
    def __init__(self) -> None:
        super().__init__(NOT_CONNECTED)


class GoogleSignInRejected(GoogleError):
    def __init__(self) -> None:
        super().__init__(NEEDS_SIGN_IN)


class GoogleRequestFailed(GoogleError):
    """A Google call failed; ``never_sent`` is True only when it provably had no effect.

    That holds when the connection never opened or Google answered 4xx. A read
    timeout, a 5xx, or an answer that did not decode leaves the effect unknown.
    """

    def __init__(self, status: int | None = None, *, never_sent: bool = False) -> None:
        self.status = status
        self.never_sent = never_sent or (status is not None and 400 <= status < 500)
        super().__init__(UNREACHABLE if status is None else f"Google refused the request (HTTP {status}).")


def id_token_subject(value: object) -> str | None:
    """Read a non-empty ``sub`` claim from Google's token response, if present."""
    if not isinstance(value, str):
        return None
    pieces = value.split(".")
    if len(pieces) != 3:
        return None
    try:
        payload = base64.urlsafe_b64decode(pieces[1] + "=" * (-len(pieces[1]) % 4))
        claims = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return None
    subject = claims.get("sub") if isinstance(claims, dict) else None
    return subject if isinstance(subject, str) and subject else None


def _on_account(work, account_id: str, *args: object):  # type: ignore[no-untyped-def]
    """Run one connected-account read or write on its own connection."""
    from infra.database import get_db

    db = get_db()
    try:
        return work(account_id, *args, db=db)
    finally:
        db.close()


def _usable_credentials(account_id: str, *, db) -> dict[str, str] | None:  # type: ignore[no-untyped-def]
    """The account's bundle only while it is connected: a signed-out or removing row lends nothing."""
    from connections.connected_accounts import is_connected, resolve_google_credentials

    return resolve_google_credentials(account_id, db=db) if is_connected(account_id, db=db) else None


async def access_token(account_id: str) -> str:
    """A fresh access token for this one account, for the one request that uses it.

    A refused refresh marks only this account ``needs_sign_in``; no other account changes.
    """
    from connections.connected_accounts import backfill_google_subject, mark_needs_sign_in

    stored = await run_in_db(_on_account, _usable_credentials, account_id)
    if stored is None:
        raise GoogleNotConnected()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
            response = await http.post(TOKEN_URL, data={
                "grant_type": "refresh_token",
                "client_id": stored["client_id"],
                "client_secret": stored["client_secret"],
                "refresh_token": stored["refresh_token"],
            })
    except httpx.HTTPError:
        raise GoogleRequestFailed() from None
    if response.status_code in (400, 401):
        # invalid_grant or an unauthorized client: this account's saved sign-in no longer works.
        await run_in_db(_on_account, mark_needs_sign_in, account_id, stored["refresh_token"])
        raise GoogleSignInRejected()
    if response.status_code != 200:
        raise GoogleRequestFailed(response.status_code)
    try:
        grant = response.json()
        token = grant.get("access_token")
        provider_subject = id_token_subject(grant.get("id_token"))
    except ValueError:
        token = None
        provider_subject = None
    if not isinstance(token, str) or not token:
        raise GoogleRequestFailed(response.status_code)
    if provider_subject is not None:
        await run_in_db(_on_account, backfill_google_subject, account_id, provider_subject)
    return token


async def request_json(
    method: str, url: str, *, account_id: str | None, params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None, token: str | None = None,
) -> Any:
    """One authorized JSON call to a Google API as one account, returning the decoded body.

    ``account_id`` is None only with a ``token`` for an account that has no row yet (sign-in).
    """
    from connections.connected_accounts import mark_needs_sign_in

    if token is None and account_id is None:
        raise GoogleNotConnected()
    bearer = token or await access_token(str(account_id))
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
            response = await http.request(
                method, url, params=params, json=body, headers={"Authorization": f"Bearer {bearer}"},
            )
    except httpx.HTTPError as exc:
        raise GoogleRequestFailed(never_sent=isinstance(exc, _NEVER_SENT)) from None
    if response.status_code == 401:
        if account_id is not None:
            await run_in_db(_on_account, mark_needs_sign_in, account_id)
        raise GoogleSignInRejected()
    if response.status_code >= 400:
        raise GoogleRequestFailed(response.status_code)
    try:
        return response.json()
    except ValueError:
        raise GoogleRequestFailed(response.status_code) from None


async def get_json(account_id: str, url: str, *, params: dict[str, Any] | None = None) -> Any:
    return await request_json("GET", url, account_id=account_id, params=params)


async def post_json(
    account_id: str, url: str, body: dict[str, Any], *, params: dict[str, Any] | None = None,
) -> Any:
    return await request_json("POST", url, account_id=account_id, params=params, body=body)
