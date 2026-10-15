"""Google sign-in: authorization-code flow with PKCE and a loopback redirect.

The person's own Desktop OAuth client file is read here, by the backend; the
agent passes only its path. The refresh token goes straight from Google's
answer into the key store and is never returned, logged or put in a result.
One sign-in can be pending at a time, in memory, for ten minutes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import httpx

from connections.google import client
from connections.google.client import (
    ACCOUNT_SETTING, STATUS_CONNECTED, TIMEOUT_SECONDS, TOKEN_URL, GoogleError, GoogleRequestFailed,
)
from infra import key_store
from infra.database import write_transaction
from infra.db_async import run_in_db
from infra.default_port import DEFAULT_BACKEND_PORT
from infra.settings import get_setting, set_setting
from infra.time import utc_now_iso

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
SCOPES = (
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
)
PENDING_SECONDS = 600
CLIENT_FILE_MAX_BYTES = 16_384
CALLBACK_PATH = "/api/google/oauth/callback"

NEEDS_CLIENT_FILE = (
    "No Google client is saved. If this setup already created the person's Desktop OAuth client, "
    "pass client_file: the path of its downloaded JSON. If none exists yet, create it in the "
    "person's own Google project in a browser they watch and publish it In production, then pass "
    "its file. Never create a second project or client when one already exists."
)
BAD_CLIENT_FILE = "That file isn't a Google Desktop app client file. Pass the path of the downloaded client JSON."
DENIED = "Google sign-in was refused or has expired. Call google_connect for a new link."
MISSING_SCOPES = "The person didn't allow every permission. Call google_connect and ask them to allow all of them."
LEGACY_ASSISTANT_HANDOVER_REQUIRED = (
    "An old Assistant Google sign-in is still available. "
    "Call google_adopt_assistant_sign_in instead of google_connect to move it safely."
)
HANDOVER_PENDING = (
    "An Assistant Google handover is already waiting for the person's Allow click. "
    "Complete it or wait up to 10 minutes, then call google_adopt_assistant_sign_in again; "
    "do not call google_connect."
)


class GoogleSignInError(GoogleError):
    """A sign-in step failed; the text is fixed and safe to show."""


@dataclass
class _Pending:
    state: str
    verifier: str
    client_id: str
    client_secret: str
    redirect_uri: str
    expires_at: float
    after_exchange: Callable[[], Awaitable[None]] | None = None
    store_failure_text: str | None = None
    is_handover: bool = False


_pending: _Pending | None = None


def redirect_uri() -> str:
    return f"http://127.0.0.1:{os.environ.get('BACKEND_PORT') or DEFAULT_BACKEND_PORT}{CALLBACK_PATH}"


def read_client_file(raw_path: object) -> tuple[str, str]:
    """The ``(client_id, client_secret)`` in a downloaded Desktop client JSON, or a fixed refusal."""
    from tools.conductor_api_paths import forbidden_path

    if not isinstance(raw_path, str) or not raw_path.strip():
        raise GoogleSignInError(NEEDS_CLIENT_FILE)
    try:
        # Check and read the same resolved file, so a symlink can't swap in another one.
        path = Path(raw_path.strip()).expanduser().resolve()
        if forbidden_path({"path": str(path)}, for_write=False) or path.suffix != ".json":
            raise GoogleSignInError(BAD_CLIENT_FILE)
        if not path.is_file() or path.stat().st_size > CLIENT_FILE_MAX_BYTES:
            raise GoogleSignInError(BAD_CLIENT_FILE)
        installed = json.loads(path.read_text(encoding="utf-8")).get("installed")
        client_id, client_secret = installed["client_id"], installed["client_secret"]
        return (
            key_store.validated_value(client_id),
            key_store.validated_value(client_secret),
        )
    except GoogleSignInError:
        raise
    except Exception:
        raise GoogleSignInError(BAD_CLIENT_FILE) from None


def _stored_client() -> tuple[str, str] | None:
    stored = key_store.resolve_names([key_store.GOOGLE_CLIENT_ID, key_store.GOOGLE_CLIENT_SECRET])
    if len(stored) != 2:
        return None
    return stored[key_store.GOOGLE_CLIENT_ID], stored[key_store.GOOGLE_CLIENT_SECRET]


def has_stored_client() -> bool:
    """Whether a validated Google Desktop client is available without exposing it."""
    return _stored_client() is not None


def sign_in_pending() -> bool:
    """Whether one Google Allow flow is still live and has not expired."""
    return _pending is not None and time.monotonic() < _pending.expires_at


def _store_client_file(raw_path: object) -> None:
    client_id, client_secret = read_client_file(raw_path)
    key_store.set_managed_value(key_store.GOOGLE_CLIENT_ID, client_id)
    key_store.set_managed_value(key_store.GOOGLE_CLIENT_SECRET, client_secret)


async def import_client_file(raw_path: object) -> None:
    """Save a Desktop OAuth client for a later authorized sign-in without returning it."""
    await run_in_db(_store_client_file, raw_path)


def connected_email() -> str | None:
    """The signed-in address when the sign-in is stored and still accepted, else None."""
    account = get_setting(ACCOUNT_SETTING)
    if account and account.get("status") == STATUS_CONNECTED and key_store.resolve_google() is not None:
        return account.get("email")
    return None


def _begin(
    client_file: object,
    after_exchange: Callable[[], Awaitable[None]] | None,
    store_failure_text: str | None,
    is_handover: bool,
    normal_start_blocked: Callable[[], bool] | None,
) -> str:
    """Sync part of starting a sign-in and atomically retain its callback state."""
    global _pending
    email = connected_email()
    if email:
        raise GoogleSignInError(f"Google is already connected as {email}.")
    if (
        not is_handover
        and _pending is not None
        and time.monotonic() < _pending.expires_at
        and _pending.is_handover
    ):
        raise GoogleSignInError(HANDOVER_PENDING)
    if not is_handover and normal_start_blocked is not None and normal_start_blocked():
        raise GoogleSignInError(LEGACY_ASSISTANT_HANDOVER_REQUIRED)
    credentials = read_client_file(client_file) if client_file else _stored_client()
    if credentials is None:
        raise GoogleSignInError(NEEDS_CLIENT_FILE)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    pending = _Pending(
        state=secrets.token_urlsafe(32), verifier=verifier, client_id=credentials[0],
        client_secret=credentials[1], redirect_uri=redirect_uri(),
        expires_at=time.monotonic() + PENDING_SECONDS,
        after_exchange=after_exchange, store_failure_text=store_failure_text, is_handover=is_handover,
    )
    _pending = pending
    return AUTH_URL + "?" + urlencode({
        "client_id": pending.client_id, "redirect_uri": pending.redirect_uri, "response_type": "code",
        "scope": " ".join(SCOPES), "state": pending.state, "code_challenge": challenge,
        "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent",
    })


async def start_sign_in(
    client_file: object = None,
    *,
    after_exchange: Callable[[], Awaitable[None]] | None = None,
    store_failure_text: str | None = None,
    is_handover: bool = False,
    normal_start_blocked: Callable[[], bool] | None = None,
) -> str:
    """The URL the person opens to sign in. The client's id and secret never leave the backend."""
    return await run_in_db(
        _begin,
        client_file,
        after_exchange,
        store_failure_text,
        is_handover,
        normal_start_blocked,
    )


def _take_pending(state: object) -> _Pending:
    """Consume the pending sign-in if ``state`` is its state and it hasn't expired.

    A wrong state leaves it pending: any page can make the browser GET the
    loopback callback, and that must not cancel the person's sign-in.
    """
    global _pending
    pending = _pending
    if (
        pending is None
        or not isinstance(state, str)
        or not secrets.compare_digest(state.encode(), pending.state.encode())
    ):
        raise GoogleSignInError(DENIED)
    _pending = None
    if time.monotonic() >= pending.expires_at:
        raise GoogleSignInError(DENIED)
    return pending


def _store(pending: _Pending, refresh_token: str, email: str, scopes: list[str]) -> None:
    """Atomically activate the client, refresh token and connected account record."""
    with write_transaction(None, scope="google_sign_in_store") as db:
        key_store.set_managed_value(key_store.GOOGLE_CLIENT_ID, pending.client_id, db=db)
        key_store.set_managed_value(key_store.GOOGLE_CLIENT_SECRET, pending.client_secret, db=db)
        key_store.set_managed_value(key_store.GOOGLE_REFRESH_TOKEN, refresh_token, db=db)
        set_setting(ACCOUNT_SETTING, {
            "email": email, "scopes": scopes, "connected_at": utc_now_iso(), "status": STATUS_CONNECTED,
        }, db=db)


async def complete_sign_in(code: object, state: object) -> str:
    """Finish the pending sign-in from Google's redirect; returns the connected address."""
    pending = _take_pending(state)
    if not isinstance(code, str) or not code:
        raise GoogleSignInError(DENIED)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
            response = await http.post(TOKEN_URL, data={
                "grant_type": "authorization_code", "code": code, "code_verifier": pending.verifier,
                "client_id": pending.client_id, "client_secret": pending.client_secret,
                "redirect_uri": pending.redirect_uri,
            })
    except httpx.HTTPError:
        raise GoogleRequestFailed() from None
    if response.status_code != 200:
        raise GoogleSignInError(DENIED)
    try:
        grant = response.json()
        refresh_token, access = grant["refresh_token"], grant["access_token"]
        scopes = sorted(str(grant.get("scope", "")).split())
        email = (await client.request_json("GET", PROFILE_URL, token=access))["emailAddress"]
        refresh_token = key_store.validated_value(refresh_token)
    except GoogleError:
        raise
    except Exception:
        raise GoogleSignInError(DENIED) from None
    if not set(SCOPES) <= set(scopes):
        raise GoogleSignInError(MISSING_SCOPES)
    if pending.after_exchange is not None:
        await pending.after_exchange()
    try:
        await run_in_db(_store, pending, refresh_token, str(email), scopes)
    except Exception:
        if pending.store_failure_text is not None:
            raise GoogleSignInError(pending.store_failure_text) from None
        raise
    return str(email)


def account_status() -> dict:
    """The sign-in's state for ``google_status``: never a token or a secret."""
    account = get_setting(ACCOUNT_SETTING)
    if not account or key_store.resolve_google() is None:
        return {"email": None, "scopes": [], "status": "not_connected", "poller_state": None, "last_success_at": None}
    # Imported here: the poller module imports this one.
    from events.connectors.gmail_poller import poller_status

    status = account.get("status", STATUS_CONNECTED)
    poller = poller_status()
    result = {
        "email": account.get("email"), "scopes": account.get("scopes", []),
        "status": status,
        "poller_state": poller["state"],
        "last_success_at": poller["last_success_at"],
    }
    if status == client.STATUS_NEEDS_SIGN_IN:
        result["needs_action"] = client.SIGN_IN_RECOVERY
    return result
