"""Google sign-in: authorization-code flow with PKCE and a loopback redirect, one account per flow.

The person's own Desktop OAuth client file is read here, by the backend; the
agent passes only its path. The refresh token goes straight from Google's
answer into that one account's bundle and is never returned, logged or put in
a result. Each sign-in waits in memory for ten minutes under its own state
nonce, so several can be pending at once, and a callback writes only its row.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from connections.app_reach import AppReach
from connections.connected_accounts import Actor
from connections.google import client
from connections.google.client import STATUS_CONNECTED, TIMEOUT_SECONDS, TOKEN_URL, GoogleError, GoogleRequestFailed
from infra import key_store
from infra.db_async import run_in_db
from infra.default_port import DEFAULT_BACKEND_PORT
from infra.database import get_db, write_transaction

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
# An ID token's ``sub`` is the account's identity; never a reason to refuse a sign-in.
IDENTITY_SCOPES = ("openid",)
SCOPES = REQUIRED_SCOPES + OPTIONAL_SCOPES + IDENTITY_SCOPES
PENDING_SECONDS = 600
PENDING_MAX = 8
CALLBACK_PATH = "/api/google/oauth/callback"

NEEDS_CLIENT_FILE = client.NEEDS_CLIENT_FILE
DENIED = "Google sign-in was refused or has expired. Call google_connect for a new link."
BROWSER_DENIED = "Google sign-in was refused or expired. Go back to CORSIA and try again."
MISSING_SCOPES = (
    "The person didn't allow Gmail and Calendar access. Call google_connect and ask them to allow "
    "Gmail and Calendar; Drive, Docs and Sheets are optional."
)
BROWSER_MISSING_SCOPES = "Gmail and Calendar access weren't allowed. Go back to CORSIA and try again."
ALREADY_CONNECTED = (
    "{address} is already connected{reach}. To change where it works, use Where it works… on its row in "
    "Connected apps."
)
ALREADY_NEEDS_SIGN_IN = (
    "{address} is already connected, but it needs you to sign in again. Use Sign in on its row in "
    "Connected apps."
)
CONNECTED_NOT_FOR_PROJECT = (
    "{address} is connected, but not for {project}. To add {project}, use Where it works… on its row in "
    "Connected apps."
)
CONNECTED_NOT_FOR_ALL = (
    "{address} is connected, but not for all your projects. To make it work for all of them, use Where it works… "
    "on its row in Connected apps."
)
STILL_CONNECTED = "{address} is already connected."
WRONG_ACCOUNT = (
    "You signed in as {signed_in}, but this sign-in is for {address}. Call google_connect again and "
    "sign in as {address}."
)
UNKNOWN_ACCOUNT = "No Google account has that account_id. Call google_status to list them."
GONE = "That Google account was removed during its sign-in. Call google_connect to add it again."
TOO_MANY = "Too many Google sign-ins are waiting. Finish one or wait ten minutes, then call google_connect."


GoogleSignInError = client.GoogleSignInError


@dataclass(frozen=True)
class _Pending:
    """One sign-in: ``adding`` creates ``account_id`` at the callback; otherwise it signs that row in."""

    state: str
    account_id: str
    adding: bool
    actor: Actor
    adding_thread_id: str | None
    reach: AppReach | None
    scopes: tuple[str, ...]
    verifier: str
    client_id: str
    client_secret: str
    redirect_uri: str
    created_at: float

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.created_at + PENDING_SECONDS


_pending: dict[str, _Pending] = {}
_pending_lock = threading.Lock()


def redirect_uri() -> str:
    return f"http://127.0.0.1:{os.environ.get('BACKEND_PORT') or DEFAULT_BACKEND_PORT}{CALLBACK_PATH}"


read_client_file = client.read_client_file


def _client_for(account_id: str | None, client_file: object, db) -> tuple[str, str] | None:  # type: ignore[no-untyped-def]
    """The client to sign in with: the file, else this row's saved client, else another row's."""
    from connections.connected_accounts import google_accounts, google_client_material

    if client_file:
        return client.read_client_file(client_file)
    if account_id is not None and (own := google_client_material(account_id, db=db)):
        return own
    for account in google_accounts(db=db):
        if material := google_client_material(str(account["id"]), db=db):
            return material
    return None


def _check_reconnect(account_id: str, db) -> None:  # type: ignore[no-untyped-def]
    from connections.connected_accounts import ConnectedAccountError, get_account, resolve_google_credentials

    try:
        account = get_account(account_id, db=db)
    except ConnectedAccountError:
        raise GoogleSignInError(UNKNOWN_ACCOUNT) from None
    if account["state"] == "removing":
        raise GoogleSignInError(
            GONE, browser_message="This Google account was removed from CORSIA. Go back to CORSIA and connect it again.",
        )
    if account["state"] == STATUS_CONNECTED and resolve_google_credentials(account_id, db=db) is not None:
        raise GoogleSignInError(STILL_CONNECTED.format(address=account["address"]))



def _begin(
    client_file: object, account_id: str | None, actor: Actor, adding_thread_id: str | None,
    reach: AppReach | None,
) -> tuple[str, str]:
    """Sync part of starting a sign-in: read the client and remember this flow under a new nonce."""
    if account_id is None and (adding_thread_id is None) == (reach is None):
        raise ValueError("an added account takes its reach from exactly one adding scope")
    db = get_db()
    try:
        if account_id is not None:
            _check_reconnect(account_id, db)
        credentials = _client_for(account_id, client_file, db)
    finally:
        db.close()
    if credentials is None:
        raise GoogleSignInError(NEEDS_CLIENT_FILE)
    client_id, client_secret = credentials
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    pending = _Pending(
        state=secrets.token_urlsafe(32), account_id=account_id or str(uuid.uuid4()),
        adding=account_id is None, actor=actor, adding_thread_id=adding_thread_id, reach=reach,
        scopes=SCOPES, verifier=verifier, client_id=client_id, client_secret=client_secret,
        redirect_uri=redirect_uri(), created_at=time.monotonic(),
    )
    with _pending_lock:
        for stale in [nonce for nonce, flow in _pending.items() if flow.expired]:
            _pending.pop(stale, None)
        if len(_pending) >= PENDING_MAX:
            raise GoogleSignInError(
                TOO_MANY,
                browser_message="Too many Google sign-ins are waiting. Go back to CORSIA and try again later.",
            )
        _pending[pending.state] = pending
    return AUTH_URL + "?" + urlencode({
        "client_id": pending.client_id, "redirect_uri": pending.redirect_uri, "response_type": "code",
        "scope": " ".join(pending.scopes), "state": pending.state, "code_challenge": challenge,
        "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent",
    }), pending.account_id


async def start_sign_in(
    client_file: object = None, *, actor: Actor, account_id: str | None = None,
    adding_thread_id: str | None = None, reach: AppReach | None = None,
) -> tuple[str, str]:
    """``(url, account_id)`` for one sign-in; the client's id and secret never leave the backend.

    With ``account_id`` it signs that row in again. Without it, it adds an
    account whose reach comes from ``adding_thread_id``'s scope or, for a
    person's session, the given ``reach``.
    """
    return await run_in_db(_begin, client_file, account_id, actor, adding_thread_id, reach)


def _take_pending(state: object) -> _Pending:
    """Consume the sign-in whose nonce is ``state`` if it hasn't expired.

    A wrong state leaves every flow pending: any page can make the browser GET
    the loopback callback, and that must not cancel the person's sign-in.
    """
    if not isinstance(state, str) or not state:
        raise GoogleSignInError(DENIED, browser_message=BROWSER_DENIED)
    with _pending_lock:
        match = next(
            (nonce for nonce in _pending if secrets.compare_digest(state.encode(), nonce.encode())), None,
        )
        pending = _pending.pop(match, None) if match is not None else None
    if pending is None or pending.expired:
        raise GoogleSignInError(DENIED, browser_message=BROWSER_DENIED)
    return pending


def _same_identity(account: dict, email: str, provider_subject: str | None) -> bool:
    """Google's ``sub`` when both sides have one, else the address without case."""
    if account["provider_subject"] and provider_subject:
        return account["provider_subject"] == provider_subject
    return str(account["address"]).strip().lower() == email.strip().lower()


def not_covered_reply(account: dict, adding_thread_id: str | None, db) -> str | None:  # type: ignore[no-untyped-def]
    """Explain when an existing account cannot serve the adding conversation's scope."""
    if adding_thread_id is None:
        return None
    from connections.connected_accounts import ConnectedAccountError, reach_for_adding_thread

    try:
        requested = reach_for_adding_thread(adding_thread_id, db=db)
    except ConnectedAccountError:
        return None
    reach = account["reach"]
    if not isinstance(reach, dict):
        return None
    if requested.mode == "all":
        return CONNECTED_NOT_FOR_ALL.format(address=account["address"]) if reach.get("mode") != "all" else None
    project_id = next(iter(requested.project_ids), None)
    if project_id is None or reach.get("mode") == "all" or project_id in reach.get("project_ids", []):
        return None
    project = db.execute("SELECT name FROM projects WHERE id=?", (project_id,)).fetchone()
    if project is None:
        return None
    return CONNECTED_NOT_FOR_PROJECT.format(address=account["address"], project=project["name"])


def _add(pending: _Pending, email: str, provider_subject: str | None, conn) -> None:  # type: ignore[no-untyped-def]
    from connections.connected_accounts import (
        create_google_account, create_google_account_for_thread, google_accounts,
    )

    for account in google_accounts(db=conn):
        if _same_identity(account, email, provider_subject):
            if account["state"] != STATUS_CONNECTED:
                raise GoogleSignInError(ALREADY_NEEDS_SIGN_IN.format(address=account["address"]))
            if reply := not_covered_reply(account, pending.adding_thread_id, conn):
                raise GoogleSignInError(reply)
            raise GoogleSignInError(ALREADY_CONNECTED.format(address=account["address"], reach=""))
    fields = {
        "created_by": pending.actor, "signed_in_by": pending.actor, "provider_subject": provider_subject,
        "account_id": pending.account_id, "state": STATUS_CONNECTED,
    }
    if pending.adding_thread_id is not None:
        create_google_account_for_thread(email, actor_thread_id=pending.adding_thread_id, db=conn, **fields)
    else:
        create_google_account(email, reach=pending.reach, db=conn, **fields)  # type: ignore[arg-type]


def _store(
    pending: _Pending, refresh_token: str, email: str, scopes: list[str], provider_subject: str | None,
) -> None:
    """Write only this flow's row: create it (an add) or sign it in again, with its audit."""
    from connections.connected_accounts import (
        ConnectedAccountError, get_account, mark_signed_in, record_audit, store_google_credentials,
    )

    bundle = {
        "client_id": pending.client_id, "client_secret": pending.client_secret,
        "refresh_token": refresh_token, "scopes": scopes,
    }
    with write_transaction(None, scope="google_sign_in_store") as conn:
        if pending.adding:
            _add(pending, email, provider_subject, conn)
        else:
            try:
                account = get_account(pending.account_id, db=conn)
            except ConnectedAccountError:
                raise GoogleSignInError(
                    GONE,
                    browser_message="This Google account was removed from CORSIA. Go back to CORSIA and connect it again.",
                ) from None
            if account["state"] == "removing":
                raise GoogleSignInError(
                    GONE,
                    browser_message="This Google account was removed from CORSIA. Go back to CORSIA and connect it again.",
                )
            if not _same_identity(account, email, provider_subject):
                raise GoogleSignInError(
                    WRONG_ACCOUNT.format(signed_in=email, address=account["address"]),
                    browser_message="That's a different Google account from the one you were adding. Go back to CORSIA and try again.",
                )
        store_google_credentials(pending.account_id, bundle, db=conn)
        if not pending.adding:
            mark_signed_in(
                pending.account_id, address=email, provider_subject=provider_subject, actor=pending.actor, db=conn,
            )
        record_audit(pending.account_id, "connect" if pending.adding else "sign_in", pending.actor, db=conn)


async def complete_sign_in(code: object, state: object) -> str:
    """Finish one pending sign-in from Google's redirect; returns the connected address."""
    pending = _take_pending(state)
    if not isinstance(code, str) or not code:
        raise GoogleSignInError(DENIED, browser_message=BROWSER_DENIED)
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
        raise GoogleSignInError(DENIED, browser_message=BROWSER_DENIED)
    try:
        grant = response.json()
        refresh_token, access = grant["refresh_token"], grant["access_token"]
        scopes = sorted(str(grant.get("scope", "")).split())
        provider_subject = client.id_token_subject(grant.get("id_token"))
        profile = await client.request_json("GET", PROFILE_URL, account_id=None, token=access)
        email = str(profile["emailAddress"])
        refresh_token = key_store.validated_value(refresh_token)
    except GoogleError:
        raise
    except Exception:
        raise GoogleSignInError(DENIED, browser_message=BROWSER_DENIED) from None
    if not set(REQUIRED_SCOPES) <= set(scopes):
        raise GoogleSignInError(MISSING_SCOPES, browser_message=BROWSER_MISSING_SCOPES)
    await run_in_db(_store, pending, refresh_token, email, scopes, provider_subject)
    return email


def accounts_status(visible_account_ids: set[str] | None = None) -> dict:
    """Visible Google accounts' sign-in state for ``google_status``: never a token or a secret."""
    # Imported here: the poller module imports this one.
    from connections.connected_accounts import google_accounts, google_scopes, resolve_google_credentials
    from events.connectors.gmail_poller import poller_status

    db = get_db()
    try:
        rows = []
        for account in google_accounts(db=db):
            account_id = str(account["id"])
            if visible_account_ids is not None and account_id not in visible_account_ids:
                continue
            status = str(account["state"])
            if status == STATUS_CONNECTED and resolve_google_credentials(account_id, db=db) is None:
                status = client.STATUS_NEEDS_SIGN_IN
            poller = poller_status(account_id)
            row = {
                "account_id": account_id, "email": account["address"], "status": status,
                "scopes": google_scopes(account_id, db=db), "reach": account["reach"],
                "poller_state": poller["state"], "last_success_at": poller["last_success_at"],
            }
            if status == client.STATUS_NEEDS_SIGN_IN:
                row["needs_action"] = client.SIGN_IN_RECOVERY
            rows.append(row)
    finally:
        db.close()
    return {"accounts": rows}
