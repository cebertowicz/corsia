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
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import httpx

from connections.google import client
from connections.google.client import (
    ACCOUNT_SETTING, STATUS_CONNECTED, TIMEOUT_SECONDS, TOKEN_URL, GoogleError, GoogleRequestFailed,
)
from infra import key_store
from infra.db_async import run_in_db
from infra.default_port import DEFAULT_BACKEND_PORT
from infra.settings import get_setting, set_setting
from infra.time import utc_now_iso

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
# Email and calendar are what the Google tools use, so a sign-in without them is refused.
REQUIRED_SCOPES = (
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
)
# Asked for in the same sign-in so the person needn't come back later; declining one still connects.
OPTIONAL_SCOPES = (
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
)
SCOPES = REQUIRED_SCOPES + OPTIONAL_SCOPES
PENDING_SECONDS = 600
CLIENT_FILE_MAX_BYTES = 16_384
CALLBACK_PATH = "/api/google/oauth/callback"

NEEDS_CLIENT_FILE = (
    "No Google client is saved. If this setup already created the person's Desktop OAuth client, "
    "pass client_file: the path of its downloaded JSON. If none exists yet, follow skill "
    "`google-setup`: it creates one in the person's own Google project, in a browser they watch, "
    "and publishes it In production. Never create a second project or client when one already exists."
)
BAD_CLIENT_FILE = "That file isn't a Google Desktop app client file. Pass the path of the downloaded client JSON."
DENIED = "Google sign-in was refused or has expired. Call google_connect for a new link."
MISSING_SCOPES = (
    "The person didn't allow Gmail and Calendar access. Call google_connect and ask them to allow "
    "Gmail and Calendar; Drive, Docs and Sheets are optional."
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


def connected_email() -> str | None:
    """The signed-in address when the sign-in is stored and still accepted, else None."""
    account = get_setting(ACCOUNT_SETTING)
    if account and account.get("status") == STATUS_CONNECTED and key_store.resolve_google() is not None:
        return account.get("email")
    return None


def _begin(client_file: object) -> str:
    """Sync part of starting a sign-in: refuse if connected, read the client, remember the state."""
    global _pending
    email = connected_email()
    if email:
        raise GoogleSignInError(f"Google is already connected as {email}.")
    credentials = read_client_file(client_file) if client_file else _stored_client()
    if credentials is None:
        raise GoogleSignInError(NEEDS_CLIENT_FILE)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    pending = _Pending(
        state=secrets.token_urlsafe(32), verifier=verifier, client_id=credentials[0],
        client_secret=credentials[1], redirect_uri=redirect_uri(),
        expires_at=time.monotonic() + PENDING_SECONDS,
    )
    _pending = pending
    return AUTH_URL + "?" + urlencode({
        "client_id": pending.client_id, "redirect_uri": pending.redirect_uri, "response_type": "code",
        "scope": " ".join(SCOPES), "state": pending.state, "code_challenge": challenge,
        "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent",
    })


async def start_sign_in(client_file: object = None) -> str:
    """The URL the person opens to sign in. The client's id and secret never leave the backend."""
    return await run_in_db(_begin, client_file)


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
    key_store.set_managed_value(key_store.GOOGLE_CLIENT_ID, pending.client_id)
    key_store.set_managed_value(key_store.GOOGLE_CLIENT_SECRET, pending.client_secret)
    key_store.set_managed_value(key_store.GOOGLE_REFRESH_TOKEN, refresh_token)
    set_setting(ACCOUNT_SETTING, {
        "email": email, "scopes": scopes, "connected_at": utc_now_iso(), "status": STATUS_CONNECTED,
    })


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
    if not set(REQUIRED_SCOPES) <= set(scopes):
        raise GoogleSignInError(MISSING_SCOPES)
    await run_in_db(_store, pending, refresh_token, str(email), scopes)
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
