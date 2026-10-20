"""Microsoft authorization-code PKCE sign-in for person-owned public clients.

Each flow listens on its own loopback port for ten minutes. An add creates the
row at its callback; a sign-in again (``account_id``) reuses the registration
saved in that row's key-store entry. Rows, credentials, state and audit go
through the shared account store; only the protocol is Microsoft's.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from connections.app_reach import AppReach
from connections.connected_accounts import MICROSOFT_PROVIDER, Actor
from connections.microsoft import account_store, client
from connections.microsoft.config import SCOPES
from connections.microsoft.errors import (
    ACCOUNT_NOT_FOUND, ACCOUNT_REMOVED, ALREADY_CONNECTED, ALREADY_NEEDS_SIGN_IN, CONSENT_CANCELLED,
    DIFFERENT_ACCOUNT, INPUT_REFUSAL, INTERNAL, SIGN_IN_BUSY, MicrosoftError, exchange_error_cause,
    message, redirect_codes_cause,
)
from infra.database import get_db
from infra.db_async import run_in_db
from tools.mcp_oauth_listener import CallbackListener, allowed_hosts

log = logging.getLogger(__name__)

AUTHORITY_HOST = "https://login.microsoftonline.com/"
PENDING_SECONDS = 600
PENDING_MAX = 8
#: The key a person's own failed add is kept under; an agent's is its thread id.
PERSON_ADDS = "person"
WRONG_FLOW_PAGE = "This isn't the Microsoft sign-in CORSIA is waiting for."
CONNECTED_PAGE = "Microsoft is connected. You can close this tab and go back to CORSIA."
#: OAuth protocol error names (RFC 6749 §5.2) safe to log; anything else logs as ``other``.
_PROTOCOL_ERRORS = frozenset((
    "invalid_request", "invalid_client", "invalid_grant", "unauthorized_client", "unsupported_grant_type",
    "invalid_scope", "access_denied", "consent_required", "interaction_required", "temporarily_unavailable",
))


@dataclass(frozen=True)
class _Pending:
    state: str
    account_id: str
    adding: bool
    actor: Actor
    adding_thread_id: str | None
    reach: AppReach | None
    kind: str
    authority: str
    verifier: str
    client_id: str
    tenant_id: str | None
    redirect_uri: str
    listener: CallbackListener
    created_at: float

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.created_at + PENDING_SECONDS


@dataclass(frozen=True)
class _LatestAddFailure:
    cause: str
    created_at: float

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.created_at + PENDING_SECONDS


_pending: dict[str, _Pending] = {}
_pending_expiries: dict[str, asyncio.Task[None]] = {}
_latest_add_failures: dict[str, _LatestAddFailure] = {}
_pending_lock = threading.Lock()


def _guid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value.strip())
    except (ValueError, AttributeError):
        return None
    return str(parsed)


def validate_connect_input(client_id: object, kind: object, tenant_id: object = None) -> tuple[str, str, str | None, str]:
    """Accept only authority-safe Microsoft registration input."""
    valid_client = _guid(client_id)
    if kind not in {"personal", "work", "both"} or valid_client is None:
        raise MicrosoftError(INPUT_REFUSAL)
    valid_tenant = _guid(tenant_id) if tenant_id is not None else None
    if kind == "work" and valid_tenant is None:
        raise MicrosoftError(INPUT_REFUSAL)
    if tenant_id is not None and (valid_tenant is None or kind != "work"):
        raise MicrosoftError(INPUT_REFUSAL)
    authority = "consumers" if kind == "personal" else "common" if kind == "both" else str(valid_tenant)
    return valid_client, str(kind), valid_tenant, authority


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _new_pending(
    client_id: object, kind: object, tenant_id: object, account_id: str | None, actor: Actor,
    adding_thread_id: str | None, reach: AppReach | None, listener: CallbackListener,
) -> _Pending:
    if account_id is None and (adding_thread_id is None) == (reach is None):
        raise ValueError("an added account takes its reach from exactly one adding scope")
    valid_client, valid_kind, valid_tenant, authority = validate_connect_input(client_id, kind, tenant_id)
    return _Pending(
        state=secrets.token_urlsafe(32), account_id=account_id or str(uuid.uuid4()), adding=account_id is None,
        actor=actor, adding_thread_id=adding_thread_id, reach=reach, kind=valid_kind, authority=authority,
        verifier=secrets.token_urlsafe(64), client_id=valid_client, tenant_id=valid_tenant,
        redirect_uri=f"http://localhost:{listener.port}", listener=listener, created_at=time.monotonic(),
    )


def _cancel_expiry(state: str) -> None:
    task = _pending_expiries.pop(state, None)
    if task is not None and task is not asyncio.current_task():
        task.cancel()


async def _expire(pending: _Pending) -> None:
    await asyncio.sleep(PENDING_SECONDS)
    with _pending_lock:
        if _pending.get(pending.state) is not pending:
            return
        _pending.pop(pending.state)
        _pending_expiries.pop(pending.state, None)
    await pending.listener.stop()


async def _close_expired() -> None:
    with _pending_lock:
        expired = [pending for pending in _pending.values() if pending.expired]
        for pending in expired:
            _pending.pop(pending.state, None)
            _cancel_expiry(pending.state)
        _clear_expired_add_failures()
    for pending in expired:
        await pending.listener.stop()


def _clear_expired_add_failures() -> None:
    for adder, failure in list(_latest_add_failures.items()):
        if failure.expired:
            _latest_add_failures.pop(adder)


def _adder(pending: _Pending) -> str:
    return pending.adding_thread_id or PERSON_ADDS


def _record_add_failure(adder: str, cause: str) -> None:
    with _pending_lock:
        _clear_expired_add_failures()
        if adder not in _latest_add_failures and len(_latest_add_failures) >= PENDING_MAX:
            oldest = min(_latest_add_failures, key=lambda key: _latest_add_failures[key].created_at)
            _latest_add_failures.pop(oldest)
        _latest_add_failures[adder] = _LatestAddFailure(cause=cause, created_at=time.monotonic())


def _clear_add_failure(adder: str) -> None:
    with _pending_lock:
        _latest_add_failures.pop(adder, None)


def _redirect_error(query: dict[str, str]) -> MicrosoftError:
    """Classify a callback error without retaining any provider-supplied text."""
    aadsts_codes = {int(match.group(1)) for value in query.values() for match in re.finditer(r"AADSTS(\d+)", value)}
    cancelled = query.get("error") == "access_denied" and query.get("error_subcode") == "cancel"
    return MicrosoftError(redirect_codes_cause(aadsts_codes, cancelled=cancelled))


async def _record_failure(pending: _Pending, error: MicrosoftError) -> None:
    """Keep a failure where the person and agents can read it: on the row being signed in again,
    or as the adder's latest failed add. Results about the request or CORSIA itself mark nothing."""
    if pending.adding:
        _record_add_failure(_adder(pending), error.cause)
        return
    if error.state_cause is not None:
        from connections.account_state import mark_sign_in_failed

        await run_in_db(mark_sign_in_failed, pending.account_id, error.state_cause)


async def start_sign_in(
    client_id: object, kind: object, tenant_id: object = None, *, actor: Actor, account_id: str | None = None,
    adding_thread_id: str | None = None, reach: AppReach | None = None,
) -> tuple[str, str]:
    """Open a one-flow loopback listener, then return the Microsoft authorization link.

    With ``account_id`` it signs that row in again with the registration saved for it;
    ``client_id``, ``kind`` and ``tenant_id`` are then ignored, and ``adding_thread_id``
    (None for the person) must be able to see the row. Without it, it adds an account
    whose reach comes from ``adding_thread_id``'s scope or, for the person, ``reach``.
    """
    if account_id is not None:
        client_id, kind, tenant_id = await run_in_db(account_store.saved_registration, account_id, adding_thread_id)
    await _close_expired()
    holder: dict[str, _Pending] = {}

    async def handler(query: dict[str, str]) -> tuple[int, str, bool, str | None]:
        pending = holder.get("pending")
        if pending is None:
            return 400, WRONG_FLOW_PAGE, False, "pending"
        given = query.get("state", "")
        if not given or not secrets.compare_digest(given, pending.state):
            return 400, WRONG_FLOW_PAGE, False, "state"
        try:
            if "error" in query:
                _take_pending(given)
                raise _redirect_error(query)
            await _complete_sign_in(query.get("code"), given)
        except MicrosoftError as exc:
            await _record_failure(pending, exc)
            return 400, exc.browser_message, True, None
        except Exception as exc:
            # A fault in CORSIA, not Microsoft: logged by type only, since its text may carry account data.
            log.error("Microsoft sign-in callback failed inside CORSIA: %s", type(exc).__name__)
            internal = MicrosoftError(INTERNAL)
            await _record_failure(pending, internal)
            return 500, internal.browser_message, True, None
        if pending.adding:
            _clear_add_failure(_adder(pending))
        log.info("Microsoft account %s signed in", pending.account_id)
        return 200, CONNECTED_PAGE, True, None

    async def on_cap() -> None:
        pending = holder.get("pending")
        if pending is not None:
            await _discard_pending(pending)

    listener = CallbackListener(path="/", allowed_hosts=allowed_hosts("localhost"), handler=handler, on_cap=on_cap)
    try:
        await listener.start()
        pending = _new_pending(client_id, kind, tenant_id, account_id, actor, adding_thread_id, reach, listener)
        holder["pending"] = pending
        with _pending_lock:
            if len(_pending) >= PENDING_MAX:
                raise MicrosoftError(SIGN_IN_BUSY)
            _pending[pending.state] = pending
            _pending_expiries[pending.state] = asyncio.create_task(_expire(pending))
    except BaseException:
        await listener.stop()
        raise
    url = AUTHORITY_HOST + pending.authority + "/oauth2/v2.0/authorize?" + urlencode({
        "client_id": pending.client_id, "response_type": "code", "redirect_uri": pending.redirect_uri,
        "response_mode": "query", "scope": " ".join(SCOPES), "state": pending.state,
        "code_challenge": _challenge(pending.verifier), "code_challenge_method": "S256",
    })
    return url, pending.account_id


async def _discard_pending(pending: _Pending) -> None:
    with _pending_lock:
        if _pending.get(pending.state) is pending:
            _pending.pop(pending.state)
            _cancel_expiry(pending.state)
    await pending.listener.stop()


async def clear_pending(account_id: str) -> None:
    with _pending_lock:
        removed = [pending for pending in _pending.values() if pending.account_id == account_id]
    for pending in removed:
        await _discard_pending(pending)


async def stop_all_signins() -> None:
    """Close all one-flow listeners when the backend shuts down."""
    with _pending_lock:
        pending = list(_pending.values())
    for flow in pending:
        await _discard_pending(flow)


def _take_pending(state: object) -> _Pending:
    if not isinstance(state, str) or not state:
        raise MicrosoftError(CONSENT_CANCELLED)
    with _pending_lock:
        match = next((key for key in _pending if secrets.compare_digest(key, state)), None)
        pending = _pending.pop(match, None) if match else None
        if match is not None:
            _cancel_expiry(match)
    if pending is None or pending.expired:
        raise MicrosoftError(CONSENT_CANCELLED)
    return pending


async def _complete_sign_in(code: object, state: object) -> str:
    pending = _take_pending(state)
    if not isinstance(code, str) or not code:
        raise MicrosoftError(CONSENT_CANCELLED)
    try:
        async with httpx.AsyncClient(timeout=client.TIMEOUT_SECONDS) as http:
            response = await http.post(client.authority_url(pending.authority), data={
                "client_id": pending.client_id, "grant_type": "authorization_code", "code": code,
                "redirect_uri": pending.redirect_uri, "code_verifier": pending.verifier, "scope": " ".join(SCOPES),
            })
    except httpx.HTTPError:
        raise MicrosoftError(exchange_error_cause(None)) from None
    try:
        grant = response.json()
    except ValueError:
        grant = None
    if response.status_code != 200 or not isinstance(grant, dict):
        error = grant.get("error") if isinstance(grant, dict) else None
        log.warning(
            "Microsoft code exchange refused: HTTP %s, %s", response.status_code,
            error if error in _PROTOCOL_ERRORS else "other",
        )
        raise MicrosoftError(exchange_error_cause(grant))
    access, refresh = grant.get("access_token"), grant.get("refresh_token")
    if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
        raise MicrosoftError(exchange_error_cause(None))
    identity = await client.identity(access, kind=pending.kind, tenant_id=pending.tenant_id)
    await run_in_db(account_store.store, pending, grant, identity)
    from events.connectors.mail_poller import schedule_account

    schedule_account(pending.account_id)
    return identity.address


def accounts_status(thread_id: str | None = None, account_id: object = None) -> dict[str, object]:
    """List the caller's reachable Microsoft rows, or report one, without exposing credentials.

    With no rows, the caller's latest failed add (``thread_id`` None is the person) is
    reported as its fixed cause and message.
    """
    from connections.connected_accounts import visible_accounts

    db = get_db()
    try:
        rows = [{
            "account_id": account["id"], "address": account["address"], "kind": account["account_kind"],
            "state": account["state"], "cause": account["state_cause"], "reach": account["reach"],
        } for account in visible_accounts(thread_id, db=db, provider=MICROSOFT_PROVIDER)]
    finally:
        db.close()
    if account_id is not None:
        selected = next((row for row in rows if row["account_id"] == account_id), None)
        return selected or {"status": "not_connected"}
    if rows:
        return {"accounts": rows}
    with _pending_lock:
        _clear_expired_add_failures()
        failure = _latest_add_failures.get(thread_id or PERSON_ADDS)
    if failure is None:
        return {"status": "not_connected"}
    return {"status": "not_connected", "cause": failure.cause, "message": message(failure.cause)}
