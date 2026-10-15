"""A minimal authorized Google HTTP client over the key store's sign-in.

Every error here carries fixed text and never a token, a secret or Google's
response body: callers may hand the message straight to an agent.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from infra import key_store
from infra.db_async import run_in_db
from infra.settings import get_setting, set_setting

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
ACCOUNT_SETTING = "google_account"
STATUS_CONNECTED = "connected"
STATUS_NEEDS_SIGN_IN = "needs_sign_in"
TIMEOUT_SECONDS = 20.0

NOT_CONNECTED = "Google isn't connected. Call google_connect and give the person the link."
NEEDS_SIGN_IN = "Google rejected the saved sign-in. Call google_connect and give the person the link."
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


def _mark_needs_sign_in() -> None:
    account = get_setting(ACCOUNT_SETTING)
    if account and account.get("status") != STATUS_NEEDS_SIGN_IN:
        set_setting(ACCOUNT_SETTING, {**account, "status": STATUS_NEEDS_SIGN_IN})


async def access_token() -> str:
    """A fresh access token from the stored refresh token, for the one request that uses it."""
    stored = await run_in_db(key_store.resolve_google)
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
        # invalid_grant or an unauthorized client: the saved sign-in no longer works.
        await run_in_db(_mark_needs_sign_in)
        raise GoogleSignInRejected()
    if response.status_code != 200:
        raise GoogleRequestFailed(response.status_code)
    try:
        token = response.json().get("access_token")
    except ValueError:
        token = None
    if not isinstance(token, str) or not token:
        raise GoogleRequestFailed(response.status_code)
    return token


async def request_json(
    method: str, url: str, *, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None,
    token: str | None = None,
) -> Any:
    """One authorized JSON call to a Google API, returning the decoded body."""
    bearer = token or await access_token()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
            response = await http.request(
                method, url, params=params, json=body, headers={"Authorization": f"Bearer {bearer}"},
            )
    except httpx.HTTPError as exc:
        raise GoogleRequestFailed(never_sent=isinstance(exc, _NEVER_SENT)) from None
    if response.status_code == 401:
        await run_in_db(_mark_needs_sign_in)
        raise GoogleSignInRejected()
    if response.status_code >= 400:
        raise GoogleRequestFailed(response.status_code)
    try:
        return response.json()
    except ValueError:
        raise GoogleRequestFailed(response.status_code) from None


async def get_json(url: str, *, params: dict[str, Any] | None = None) -> Any:
    return await request_json("GET", url, params=params)


async def post_json(url: str, body: dict[str, Any], *, params: dict[str, Any] | None = None) -> Any:
    return await request_json("POST", url, params=params, body=body)
