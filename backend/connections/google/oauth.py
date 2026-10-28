"""Google sign-in using the published Workspace client or a person's Desktop client.

Each flow has its own nonce and stores its refresh token in the account's key-store bundle.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlencode

from connections.app_reach import AppReach
from connections.account_state import CONSENT_CANCELLED, PROVIDER_UNAVAILABLE, emit_lifecycle_event
from connections.connected_accounts import Actor
from connections.google import client
from connections.google.client import STATUS_CONNECTED, GoogleError
from connections.google.reach_replies import (
    ALREADY_CONNECTED, ALREADY_NEEDS_SIGN_IN, STILL_CONNECTED, not_covered_reply,
)
from connections.sign_in_notice import (
    SHUT_DOWN_MESSAGE, drive_starting_agent, notify_ended, notify_expired, notify_starting_agent, wake_on_connect,
)
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
PUBLISHED_REQUIRED_SCOPES = ("https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/calendar")
PUBLISHED_SCOPES = PUBLISHED_REQUIRED_SCOPES + ("https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/documents")
PUBLISHED_CLIENT_ID = "338689075775-o75k922vn5fdl18qergr96rp8g63e4d7.apps.googleusercontent.com"
PUBLISHED_URL = client.PUBLISHED_URL
PENDING_SECONDS = 600
PENDING_MAX = 8
CALLBACK_PATH = "/api/google/oauth/callback"

NEEDS_CLIENT_FILE = client.NEEDS_CLIENT_FILE
DENIED = "Google sign-in was refused or has expired. Call google_connect for a new link."
BROWSER_DENIED = "Google sign-in was refused or expired. Go back to CORSIA and try again."
_MISSING_SCOPES_TEMPLATE = (
    "The person didn't allow Gmail and Calendar access. Call google_connect and ask them to allow "
    "Gmail and Calendar; {optional} are optional."
)
MISSING_SCOPES = _MISSING_SCOPES_TEMPLATE.format(optional="Drive, Docs and Sheets")
PUBLISHED_MISSING_SCOPES = _MISSING_SCOPES_TEMPLATE.format(optional="Drive and Docs")
BROWSER_MISSING_SCOPES = "Gmail and Calendar access weren't allowed. Go back to CORSIA and try again."
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
    starting_thread_id: str | None
    reach: AppReach | None
    scopes: tuple[str, ...]
    verifier: str | None
    client: str
    client_id: str
    client_secret: str | None
    redirect_uri: str
    created_at: float

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.created_at + PENDING_SECONDS


_pending: dict[str, _Pending] = {}
_expiries: dict[str, asyncio.Task[None]] = {}
_pending_lock = threading.Lock()


def redirect_uri() -> str:
    return f"http://127.0.0.1:{os.environ.get('BACKEND_PORT') or DEFAULT_BACKEND_PORT}{CALLBACK_PATH}"


read_client_file = client.read_client_file


_client_for = client.client_for


def _check_reconnect(account_id: str, db) -> None:  # type: ignore[no-untyped-def]
    from connections.connected_accounts import ConnectedAccountError, get_account, resolve_credentials

    try:
        account = get_account(account_id, db=db)
    except ConnectedAccountError:
        raise GoogleSignInError(UNKNOWN_ACCOUNT) from None
    if account["state"] == STATUS_CONNECTED and resolve_credentials(account_id, db=db) is not None:
        raise GoogleSignInError(STILL_CONNECTED.format(address=account["address"]))


def _begin(
    client_file: object, account_id: str | None, actor: Actor, adding_thread_id: str | None,
    reach: AppReach | None, starting_thread_id: str | None = None,
) -> tuple[str, _Pending]:
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
    kind = "own" if credentials is not None else "published"
    published = client.uses_published({"client": kind})
    if published:
        client_id, client_secret = PUBLISHED_CLIENT_ID, None
    else:
        assert credentials is not None
        client_id, client_secret = credentials
    verifier = None if published else secrets.token_urlsafe(64)
    pending = _Pending(
        state=secrets.token_urlsafe(32), account_id=account_id or str(uuid.uuid4()),
        adding=account_id is None, actor=actor, adding_thread_id=adding_thread_id, reach=reach,
        starting_thread_id=starting_thread_id, scopes=PUBLISHED_SCOPES if published else SCOPES,
        verifier=verifier, client=kind, client_id=client_id,
        client_secret=client_secret, redirect_uri=redirect_uri(), created_at=time.monotonic(),
    )
    with _pending_lock:
        if len(_pending) >= PENDING_MAX:
            raise GoogleSignInError(
                TOO_MANY,
                browser_message="Too many Google sign-ins are waiting. Go back to CORSIA and try again later.",
            )
        _pending[pending.state] = pending
    params = {
        "client_id": pending.client_id, "redirect_uri": pending.redirect_uri, "response_type": "code",
        "scope": " ".join(pending.scopes), "state": pending.state, "access_type": "offline", "prompt": "consent",
    }
    if client.uses_published(pending):
        params["redirect_uri"] = PUBLISHED_URL
        params["state"] = base64.b64encode(json.dumps({
            "uri": pending.redirect_uri, "manual": False, "csrf": pending.state,
        }).encode()).decode()
    else:
        assert verifier is not None
        params["code_challenge"] = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        params["code_challenge_method"] = "S256"
    return AUTH_URL + "?" + urlencode(params), pending


async def start_sign_in(
    client_file: object = None, *, actor: Actor, account_id: str | None = None,
    adding_thread_id: str | None = None, starting_thread_id: str | None = None, reach: AppReach | None = None,
) -> tuple[str, str]:
    """``(url, account_id)`` for one sign-in; a client's secret never leaves the backend.

    With ``account_id`` it signs that row in again. Without it, it adds an account whose reach comes from
    ``adding_thread_id``'s scope or, for a person's session, the given ``reach``. ``starting_thread_id`` is the agent
    thread that started the sign-in, either kind; it is woken once with how it ended, even if nobody finishes it.
    """
    url, pending = await run_in_db(
        _begin, client_file, account_id, actor, adding_thread_id, reach, starting_thread_id,
    )
    _expiries[pending.state] = asyncio.create_task(_expire(pending))
    emit_lifecycle_event("connect_started", pending.account_id, "google")
    return url, pending.account_id


async def _expire(pending: _Pending) -> None:
    """Tell the starting agent its sign-in ran out, unless the callback took the flow first."""
    await asyncio.sleep(PENDING_SECONDS)
    with _pending_lock:
        if _pending.pop(pending.state, None) is None:
            return
        _expiries.pop(pending.state, None)
    await notify_expired(pending.starting_thread_id, pending.state, "google", pending.account_id)


async def stop_all_signins() -> None:
    """Drop every pending sign-in at shutdown; each waiting agent is woken, but no turn starts."""
    with _pending_lock:
        flows = list(_pending.values())
        _pending.clear()
        for state in list(_expiries):
            _expiries.pop(state).cancel()
    for flow in flows:
        await notify_ended(flow.starting_thread_id, flow.state, "google", flow.account_id, SHUT_DOWN_MESSAGE, drive=False)


def _take_pending(state: object) -> _Pending:
    """Consume the unexpired sign-in whose nonce is ``state``; none matching is DENIED.

    A wrong state leaves every flow pending: any page can make the browser GET
    the loopback callback, and that must not cancel the person's sign-in.
    """
    if not isinstance(state, str) or not state:
        raise GoogleSignInError(DENIED, browser_message=BROWSER_DENIED)
    with _pending_lock:
        match = next(
            (nonce for nonce in _pending if secrets.compare_digest(state.encode(), nonce.encode())), None,
        )
        pending = _pending.get(match) if match is not None else None
        if pending is not None and pending.expired:
            raise GoogleSignInError(DENIED, browser_message=BROWSER_DENIED)
        if match is not None:
            _pending.pop(match)
        timer = _expiries.pop(match, None) if match is not None else None
    if timer is not None:
        timer.cancel()
    if pending is None:
        raise GoogleSignInError(DENIED, browser_message=BROWSER_DENIED)
    return pending


def _same_identity(account: dict, email: str, provider_subject: str | None) -> bool:
    """Google's ``sub`` when both sides have one, else the address without case."""
    if account["provider_subject"] and provider_subject:
        return account["provider_subject"] == provider_subject
    return str(account["address"]).strip().lower() == email.strip().lower()


def _add(pending: _Pending, email: str, provider_subject: str | None, conn) -> None:  # type: ignore[no-untyped-def]
    from connections.connected_accounts import GOOGLE_PROVIDER, create_account, create_account_for_thread, list_accounts

    for account in list_accounts(GOOGLE_PROVIDER, db=conn):
        if _same_identity(account, email, provider_subject):
            # These replies address the person, so the callback page shows them as they are.
            if account["state"] != STATUS_CONNECTED:
                reply = ALREADY_NEEDS_SIGN_IN.format(address=account["address"])
            else:
                reply = not_covered_reply(account, pending.adding_thread_id, conn) or ALREADY_CONNECTED.format(
                    address=account["address"], reach="",
                )
            raise GoogleSignInError(reply, browser_message=reply)
    fields = {
        "created_by": pending.actor, "signed_in_by": pending.actor, "provider_subject": provider_subject,
        "account_id": pending.account_id, "state": STATUS_CONNECTED,
    }
    if pending.adding_thread_id is not None:
        create_account_for_thread(GOOGLE_PROVIDER, email, actor_thread_id=pending.adding_thread_id, db=conn, **fields)
    else:
        create_account(GOOGLE_PROVIDER, email, reach=pending.reach, db=conn, **fields)  # type: ignore[arg-type]


def _store(
    pending: _Pending, refresh_token: str, email: str, scopes: list[str], provider_subject: str | None,
) -> None:
    """Write only this flow's row: create it (an add) or sign it in again, with its audit."""
    from connections.connected_accounts import ConnectedAccountError, get_account, store_credentials
    from connections.account_state import mark_signed_in, record_audit

    bundle: dict[str, object] = {"client": pending.client, "client_id": pending.client_id, "refresh_token": refresh_token, "scopes": scopes}
    if not client.uses_published(pending):
        bundle["client_secret"] = pending.client_secret
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
            if not _same_identity(account, email, provider_subject):
                raise GoogleSignInError(
                    WRONG_ACCOUNT.format(signed_in=email, address=account["address"]),
                    browser_message=f"That's a different Google account. Go back to CORSIA and sign in again as {account['address']}.",
                )
        store_credentials(pending.account_id, bundle, db=conn)
        if not pending.adding:
            mark_signed_in(
                pending.account_id, address=email, provider_subject=provider_subject, actor=pending.actor, db=conn,
            )
        record_audit(pending.account_id, "connect" if pending.adding else "sign_in", pending.actor, db=conn)
        wake_on_connect(conn, pending.starting_thread_id, pending.state, "google", pending.account_id, email)


def _failure_cause(error: GoogleError) -> str:
    return CONSENT_CANCELLED if isinstance(error, GoogleSignInError) else PROVIDER_UNAVAILABLE


async def _tell_agent(pending: _Pending, *, cause: str, message: str | None = None) -> None:
    await notify_starting_agent(
        pending.starting_thread_id, pending.state, "google", pending.account_id, cause=cause, message=message,
    )


async def complete_sign_in(code: object, state: object, *, access_token: object = None,
                           refresh_token: object = None, scope: object = None) -> str:
    """Finish one pending sign-in from Google's redirect; returns the connected address."""
    pending = _take_pending(state)
    try:
        published = client.uses_published(pending)
        if not published:
            from connections.google.oauth_exchange import exchange

            grant = await exchange(
                code, verifier=pending.verifier, client_id=pending.client_id, client_secret=pending.client_secret,
                redirect_uri=pending.redirect_uri, denied=DENIED, browser_denied=BROWSER_DENIED,
            )
        else:
            grant = {"access_token": access_token, "refresh_token": refresh_token, "scope": scope}
        try:
            refresh_token = key_store.validated_value(grant["refresh_token"])
            access = key_store.validated_value(grant["access_token"])
            scopes = sorted(str(grant.get("scope") or "").split())
            required = PUBLISHED_REQUIRED_SCOPES if published else REQUIRED_SCOPES
            if not set(required) <= set(scopes):
                message = PUBLISHED_MISSING_SCOPES if published else MISSING_SCOPES
                raise GoogleSignInError(message, browser_message=BROWSER_MISSING_SCOPES)
            provider_subject = await client.token_subject(access) if published else client.id_token_subject(grant.get("id_token"))
            profile = await client.request_json("GET", PROFILE_URL, account_id=None, token=access)
            email = str(profile["emailAddress"])
        except GoogleError:
            raise
        except Exception:
            raise GoogleSignInError(DENIED, browser_message=BROWSER_DENIED) from None
        await run_in_db(_store, pending, refresh_token, email, scopes, provider_subject)
        await drive_starting_agent(pending.starting_thread_id, pending.state)
        from events.connectors.mail_poller import schedule_account

        schedule_account(pending.account_id)
    except GoogleError as error:
        cause = _failure_cause(error)
        emit_lifecycle_event("connect_failed", pending.account_id, "google", cause=cause)
        await _tell_agent(pending, cause=cause, message=str(error))
        raise
    except Exception:
        emit_lifecycle_event("connect_failed", pending.account_id, "google", cause=PROVIDER_UNAVAILABLE)
        await _tell_agent(pending, cause=PROVIDER_UNAVAILABLE)
        raise
    emit_lifecycle_event("connect_succeeded", pending.account_id, "google")
    return email


def accounts_status(thread_id: str | None = None) -> dict:
    """Google accounts' sign-in state, never a token or a secret: every account for the person
    (``thread_id`` None), only those that reach the calling agent's thread otherwise. Both
    ``/api/google/status`` and the ``google_status`` tool answer from here."""
    # Imported here: the poller module imports this one.
    from connections.connected_accounts import GOOGLE_PROVIDER, granted_scopes, resolve_credentials, visible_accounts
    from events.connectors.mail_poller import poller_status

    db = get_db()
    try:
        rows = []
        for account in visible_accounts(thread_id, db=db, provider=GOOGLE_PROVIDER):
            account_id = str(account["id"])
            status = str(account["state"])
            if status == STATUS_CONNECTED and resolve_credentials(account_id, db=db) is None:
                status = client.STATUS_NEEDS_SIGN_IN
            poller = poller_status(account_id)
            row = {
                "account_id": account_id, "email": account["address"], "status": status,
                "scopes": granted_scopes(account_id, db=db), "reach": account["reach"],
                "poller_state": poller["state"], "last_success_at": poller["last_success_at"],
            }
            if status == client.STATUS_NEEDS_SIGN_IN:
                row["needs_action"] = client.sign_in_recovery(account_id, db=db)
            rows.append(row)
    finally:
        db.close()
    return {"accounts": rows}
