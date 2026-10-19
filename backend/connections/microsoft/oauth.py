"""Microsoft authorization-code PKCE sign-in for person-owned public clients."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from connections.app_reach import AppReach
from connections.connected_accounts import Actor
from connections.microsoft import client
from connections.microsoft.config import CONSUMER_TENANT_ID
from connections.microsoft.errors import (
    ADMIN_APPROVAL_REQUIRED, CONSENT_CANCELLED, INPUT_REFUSAL, PROVIDER_UNAVAILABLE, MicrosoftError, token_error_cause,
)
from infra import key_store
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db
from tools.mcp_oauth_listener import CallbackListener, allowed_hosts

AUTHORITY_HOST = "https://login.microsoftonline.com/"
SCOPES = ("Mail.ReadWrite", "Mail.Send", "Calendars.ReadWrite", "Files.ReadWrite", "offline_access", "User.Read")
PENDING_SECONDS = 600
PENDING_MAX = 8
# The consumer tenant identifier lives with the connector's static configuration.


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
    authority = "consumers" if kind == "personal" else "common" if kind == "both" else valid_tenant
    assert authority is not None
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
    for thread_id, failure in list(_latest_add_failures.items()):
        if failure.expired:
            _latest_add_failures.pop(thread_id)


def _record_add_failure(thread_id: str, cause: str) -> None:
    with _pending_lock:
        _clear_expired_add_failures()
        if thread_id not in _latest_add_failures and len(_latest_add_failures) >= PENDING_MAX:
            oldest = min(_latest_add_failures, key=lambda key: _latest_add_failures[key].created_at)
            _latest_add_failures.pop(oldest)
        _latest_add_failures[thread_id] = _LatestAddFailure(cause=cause, created_at=time.monotonic())


def _clear_add_failure(thread_id: str) -> None:
    with _pending_lock:
        _latest_add_failures.pop(thread_id, None)


def _redirect_error_cause(query: dict[str, str]) -> str:
    """Classify a callback error without retaining any provider-supplied text."""
    aadsts_codes = {int(match.group(1)) for value in query.values() for match in re.finditer(r"AADSTS(\d+)", value)}
    # Microsoft confirms AADSTS65004 is declined consent, never administrator approval.
    if (query.get("error") == "access_denied" and query.get("error_subcode") == "cancel") or 65004 in aadsts_codes:
        return CONSENT_CANCELLED
    if aadsts_codes & {90094, 90095, 65001}:
        return ADMIN_APPROVAL_REQUIRED
    return PROVIDER_UNAVAILABLE


async def _record_failure(pending: _Pending, cause: str) -> None:
    if pending.adding:
        if pending.adding_thread_id is not None:
            _record_add_failure(pending.adding_thread_id, cause)
        return
    from connections.microsoft.accounts import mark_microsoft_needs_sign_in

    await run_in_db(mark_microsoft_needs_sign_in, pending.account_id, cause=cause)


async def start_sign_in(
    client_id: object, kind: object, tenant_id: object = None, *, actor: Actor, account_id: str | None = None,
    adding_thread_id: str | None = None, reach: AppReach | None = None,
) -> tuple[str, str]:
    """Open a one-flow loopback listener, then return the Microsoft authorization link."""
    await _close_expired()
    holder: dict[str, _Pending] = {}

    async def handler(query: dict[str, str]) -> tuple[int, str, bool, str | None]:
        pending = holder.get("pending")
        if pending is None:
            return 400, "This isn't the Microsoft sign-in CORSIA is waiting for.", False, "pending"
        given = query.get("state", "")
        if not given or not secrets.compare_digest(given, pending.state):
            return 400, "This isn't the Microsoft sign-in CORSIA is waiting for.", False, "state"
        if "error" in query:
            pending = _take_pending(given)
            await _record_failure(pending, _redirect_error_cause(query))
            return 400, "Microsoft sign-in couldn't finish. Go back to CORSIA and try again.", True, None
        try:
            await _complete_sign_in(query.get("code"), given)
        except MicrosoftError as exc:
            await _record_failure(pending, exc.cause)
            return 400, exc.browser_message or "Microsoft sign-in couldn't finish. Go back to CORSIA and try again.", True, None
        except Exception:
            await _record_failure(pending, PROVIDER_UNAVAILABLE)
            return 500, "Microsoft sign-in couldn't finish. Go back to CORSIA and try again.", True, None
        if pending.adding_thread_id is not None:
            _clear_add_failure(pending.adding_thread_id)
        return 200, "Microsoft is connected. You can close this tab and go back to CORSIA.", True, None

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
                raise MicrosoftError(PROVIDER_UNAVAILABLE)
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
        raise MicrosoftError(CONSENT_CANCELLED, browser_message="Microsoft sign-in was cancelled. Go back to CORSIA and try again.")
    with _pending_lock:
        match = next((key for key in _pending if secrets.compare_digest(key, state)), None)
        pending = _pending.pop(match, None) if match else None
        if match is not None:
            _cancel_expiry(match)
    if pending is None or pending.expired:
        raise MicrosoftError(CONSENT_CANCELLED, browser_message="Microsoft sign-in was cancelled or expired. Go back to CORSIA and try again.")
    return pending


def _provider_subject(tenant_id: str, object_id: str) -> str:
    return f"{tenant_id}:{object_id}"


def _store(pending: _Pending, grant: dict[str, object], tenant: str, object_id: str, address: str) -> None:
    from connections.connected_accounts import ConnectedAccountError, get_account, record_audit
    from connections.microsoft.accounts import (
        create_microsoft_account,
        create_microsoft_account_for_thread,
        mark_microsoft_signed_in,
        store_microsoft_credentials,
    )
    row_kind = "personal" if pending.kind == "personal" or tenant.lower() == CONSUMER_TENANT_ID else "work"
    subject = _provider_subject(tenant, object_id)
    bundle = {
        "access_token": key_store.validated_value(grant["access_token"]),
        "refresh_token": key_store.validated_value(grant["refresh_token"]),
        "client_id": pending.client_id, "tenant_id": "consumers" if row_kind == "personal" else tenant,
        "scopes": list(SCOPES),
    }
    with write_transaction(None, scope="microsoft_sign_in_store") as conn:
        if pending.adding:
            existing = conn.execute(
                "SELECT id FROM connected_accounts WHERE provider='microsoft' AND provider_subject=?", (subject,),
            ).fetchone()
            if existing is not None:
                raise MicrosoftError(CONSENT_CANCELLED)
            fields = {"created_by": pending.actor, "signed_in_by": pending.actor, "provider_subject": subject,
                      "account_kind": row_kind, "account_id": pending.account_id, "state": "connected"}
            if pending.adding_thread_id is not None:
                create_microsoft_account_for_thread(address, actor_thread_id=pending.adding_thread_id, db=conn, **fields)
            else:
                create_microsoft_account(address, reach=pending.reach, db=conn, **fields)  # type: ignore[arg-type]
        else:
            try:
                account = get_account(pending.account_id, db=conn)
            except ConnectedAccountError:
                raise MicrosoftError(CONSENT_CANCELLED) from None
            if account["provider"] != "microsoft" or account["provider_subject"] != subject:
                raise MicrosoftError(CONSENT_CANCELLED)
            mark_microsoft_signed_in(pending.account_id, address=address, provider_subject=subject, account_kind=row_kind, actor=pending.actor, db=conn)
        store_microsoft_credentials(pending.account_id, bundle, db=conn)
        record_audit(pending.account_id, "connect" if pending.adding else "sign_in", pending.actor, db=conn)


async def _complete_sign_in(code: object, state: object) -> str:
    pending = _take_pending(state)
    if not isinstance(code, str) or not code:
        raise MicrosoftError(CONSENT_CANCELLED, browser_message="Microsoft sign-in was cancelled. Go back to CORSIA and try again.")
    try:
        async with httpx.AsyncClient(timeout=client.TIMEOUT_SECONDS) as http:
            response = await http.post(client.authority_url(pending.authority), data={
                "client_id": pending.client_id, "grant_type": "authorization_code", "code": code,
                "redirect_uri": pending.redirect_uri, "code_verifier": pending.verifier, "scope": " ".join(SCOPES),
            })
    except httpx.HTTPError:
        raise MicrosoftError(PROVIDER_UNAVAILABLE) from None
    try:
        grant = response.json()
    except ValueError:
        grant = None
    if response.status_code != 200 or not isinstance(grant, dict):
        raise MicrosoftError(token_error_cause(response.status_code, grant), browser_message="Microsoft sign-in couldn't finish. Go back to CORSIA and try again.")
    access = grant.get("access_token")
    identity = client.id_token_identity(grant.get("id_token")) or client.id_token_identity(access)
    if identity is None:
        raise MicrosoftError(PROVIDER_UNAVAILABLE, browser_message="Microsoft sign-in couldn't read the account. Go back to CORSIA and try again.")
    tenant, object_id, address = identity
    if not address:
        profile = await client.profile(str(access))
        address = profile[2] if profile is not None else None
    if not isinstance(address, str) or not address.strip() or not isinstance(grant.get("refresh_token"), str):
        raise MicrosoftError(PROVIDER_UNAVAILABLE, browser_message="Microsoft sign-in couldn't read the account. Go back to CORSIA and try again.")
    await run_in_db(_store, pending, grant, tenant, object_id, address)
    return address


def accounts_status(thread_id: str | None = None, account_id: object = None) -> dict[str, object]:
    """List reachable Microsoft rows, or report one selected row without exposing credentials."""
    from connections.microsoft.accounts import visible_microsoft_accounts
    db = get_db()
    try:
        rows = [{
            "account_id": account["id"], "address": account["address"], "kind": account["account_kind"],
            "state": account["state"], "cause": account["state_cause"], "reach": account["reach"],
        } for account in visible_microsoft_accounts(thread_id, db=db)]
        if account_id is not None:
            selected = next((row for row in rows if row["account_id"] == account_id), None)
            return selected or {"status": "not_connected"}
        if rows:
            return {"accounts": rows}
        with _pending_lock:
            _clear_expired_add_failures()
            failure = _latest_add_failures.get(thread_id) if thread_id is not None else None
        return {"status": "not_connected", "cause": failure.cause} if failure else {"status": "not_connected"}
    finally:
        db.close()
