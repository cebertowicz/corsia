"""A minimal authorized Google HTTP client, one connected account per call.

Every error here carries fixed text and never a token, a secret or Google's
response body: callers may hand the message straight to an agent.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from connections.account_errors import AccountError
from infra import key_store
from infra.db_async import run_in_db

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
PUBLISHED_REFRESH_URL = "https://google-workspace-extension.geminicli.com/refreshToken"
STATUS_CONNECTED = "connected"
STATUS_NEEDS_SIGN_IN = "needs_sign_in"
TIMEOUT_SECONDS = 20.0
CLIENT_FILE_MAX_BYTES = 16_384
NEEDS_CLIENT_FILE = (
    "No Google client is saved. If this setup already created the person's Desktop OAuth client, "
    "pass client_file: the path of its downloaded JSON. If none exists yet, follow skill "
    "`google-setup`: it creates one in the person's own Google project, in a browser they watch, "
    "and publishes it In production. Never create a second project when one already exists."
)
BAD_CLIENT_FILE = "That file isn't a Google Desktop app client file. Pass the path of the downloaded client JSON."
MISSING_CLIENT_FILE = "There's no file at that path. Pass the path of the client JSON you downloaded from Google Cloud."

NOT_CONNECTED = "Google isn't connected. Call google_connect and give the person the link."
SIGN_IN_RECOVERY = (
    'Google rejected the saved sign-in. Sign-ins for a project in Testing expire after 7 days: '
    'if the Audience page of the OAuth consent screen in Google Cloud shows Testing, ask the person '
    'to publish their app "In production", keeping the same project. Then call google_connect with '
    'this account_id and post its reply verbatim followed by its link as [Open it in Connected apps](<link>).'
)
PUBLISHED_SIGN_IN_RECOVERY = "Google rejected the saved sign-in. Call google_connect again for this account."
NEEDS_SIGN_IN = SIGN_IN_RECOVERY
UNREACHABLE = "Couldn't reach Google. Try again later."
# Transport errors raised before any request bytes reach Google.
_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class GoogleError(AccountError):
    """Base of every Google-only error this package raises; its text is safe to show."""


class GoogleNotConnected(GoogleError):
    def __init__(self) -> None:
        super().__init__(NOT_CONNECTED)


class GoogleSignInRejected(GoogleError):
    def __init__(self, *, published: bool = False) -> None:
        super().__init__(PUBLISHED_SIGN_IN_RECOVERY if published else NEEDS_SIGN_IN)


class GoogleSignInError(GoogleError):
    """An actionable error while starting or finishing a Google sign-in."""

    def __init__(self, message: str, *, browser_message: str | None = None) -> None:
        super().__init__(message)
        self.browser_message = browser_message


class GoogleRequestFailed(GoogleError):
    """A Google call failed; ``never_sent`` is True only when it provably had no effect.

    That holds when the connection never opened or Google answered 4xx. A read
    timeout, a 5xx, or an answer that did not decode leaves the effect unknown.
    """

    def __init__(self, status: int | None = None, *, never_sent: bool = False) -> None:
        self.status = status
        self.never_sent = never_sent or (status is not None and 400 <= status < 500)
        super().__init__(UNREACHABLE if status is None else f"Google refused the request (HTTP {status}).")


def read_client_file(raw_path: object) -> tuple[str, str]:
    """Read a downloaded Desktop OAuth client file, or raise an actionable sign-in error."""
    from tools.conductor_api_paths import forbidden_path

    if not isinstance(raw_path, str) or not raw_path.strip():
        raise GoogleSignInError(NEEDS_CLIENT_FILE)
    try:
        path = Path(raw_path.strip()).expanduser().resolve()
        if forbidden_path({"path": str(path)}, for_write=False):
            raise GoogleSignInError(BAD_CLIENT_FILE)
        if not path.exists():
            raise GoogleSignInError(MISSING_CLIENT_FILE)
        if path.suffix != ".json" or not path.is_file() or path.stat().st_size > CLIENT_FILE_MAX_BYTES:
            raise GoogleSignInError(BAD_CLIENT_FILE)
        installed = json.loads(path.read_text(encoding="utf-8")).get("installed")
        return key_store.validated_value(installed["client_id"]), key_store.validated_value(installed["client_secret"])
    except GoogleSignInError:
        raise
    except Exception:
        raise GoogleSignInError(BAD_CLIENT_FILE) from None


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
    """The account's bundle only while it is connected: a signed-out or removed account lends nothing."""
    from connections.connected_accounts import is_connected, resolve_credentials

    return resolve_credentials(account_id, db=db) if is_connected(account_id, db=db) else None


def sign_in_recovery(account_id: str, *, db) -> str:  # type: ignore[no-untyped-def]
    """Choose recovery instructions for the account's saved client."""
    from connections.connected_accounts import resolve_credentials

    stored = resolve_credentials(account_id, db=db)
    return PUBLISHED_SIGN_IN_RECOVERY if stored and stored.get("client") == "published" else SIGN_IN_RECOVERY


async def access_token(account_id: str) -> str:
    """A fresh access token for this one account, for the one request that uses it.

    A refused refresh marks only this account ``needs_sign_in``; no other account changes.
    """
    from connections.connected_accounts import backfill_subject
    from connections.account_state import mark_needs_sign_in

    stored = await run_in_db(_on_account, _usable_credentials, account_id)
    if stored is None:
        raise GoogleNotConnected()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
            if stored.get("client") == "published":
                response = await http.post(PUBLISHED_REFRESH_URL, json={"refresh_token": stored["refresh_token"]})
            else:
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
        raise GoogleSignInRejected(published=stored.get("client") == "published")
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
        await run_in_db(_on_account, backfill_subject, account_id, provider_subject)
    return token


async def request_json(
    method: str, url: str, *, account_id: str | None, params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None, token: str | None = None,
) -> Any:
    """One authorized JSON call to a Google API as one account, returning the decoded body.

    ``account_id`` is None only with a ``token`` for an account that has no row yet (sign-in).
    """
    from connections.account_state import mark_needs_sign_in

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
            recovery = await run_in_db(_on_account, sign_in_recovery, account_id)
            raise GoogleSignInRejected(published=recovery == PUBLISHED_SIGN_IN_RECOVERY)
        raise GoogleSignInRejected()
    if response.status_code >= 400:
        raise GoogleRequestFailed(response.status_code)
    if response.status_code == 204:
        # A ``fields`` mask that selects nothing (a search with no matches) comes back as No Content.
        return {}
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
