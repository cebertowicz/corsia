"""One connected account's lifecycle writes: its state and cause, cursor and audit trail.

Each write changes only its own row and never carries provider response text.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from typing import TYPE_CHECKING

from infra.database import write_transaction
from infra.product_events import emit_product_event
from infra.time import utc_now_iso

if TYPE_CHECKING:
    from connections.connected_accounts import Actor

APP_REGISTRATION_NOT_ALLOWED = "app_registration_not_allowed"
ADMIN_APPROVAL_REQUIRED = "admin_approval_required"
CONSENT_CANCELLED = "consent_cancelled"
REGISTRATION_MISCONFIGURED = "registration_misconfigured"
REFRESH_TOKEN_INVALID = "refresh_token_invalid"
PROVIDER_UNAVAILABLE = "provider_unavailable"
STATE_CAUSES = frozenset((
    ADMIN_APPROVAL_REQUIRED, CONSENT_CANCELLED, REGISTRATION_MISCONFIGURED, REFRESH_TOKEN_INVALID, PROVIDER_UNAVAILABLE,
))
MANUAL_ONLY = "manual_only"
INPUT_REFUSAL = "input_refusal"
AUDIT_ACTIONS = frozenset(("connect", "sign_in", "sign_out", "remove"))
REVOCATION_CAUSES = frozenset((
    "revoked", "provider_unreachable", "provider_timeout", "provider_rejected", "provider_unexpected", "not_signed_in",
    APP_REGISTRATION_NOT_ALLOWED, ADMIN_APPROVAL_REQUIRED, CONSENT_CANCELLED, REGISTRATION_MISCONFIGURED,
    REFRESH_TOKEN_INVALID, PROVIDER_UNAVAILABLE, MANUAL_ONLY, INPUT_REFUSAL,
))
LIFECYCLE_ACTIONS = frozenset(("connect_started", "connect_succeeded", "connect_failed", "signed_out", "removed"))


def emit_lifecycle_event(action: str, account_id: str, provider: str, *, cause: str | None = None) -> None:
    """Record one provider-neutral account lifecycle event after its durable transition."""
    if action not in LIFECYCLE_ACTIONS:
        raise ValueError("invalid connected account lifecycle event")
    properties: dict[str, str] = {"account_id": account_id, "provider": provider}
    if cause is not None:
        properties["cause"] = cause
    emit_product_event("connected_account", action, properties=properties)


def set_state(account_id: str, state: str, *, db: sqlite3.Connection | None = None) -> None:
    """Update one account's lifecycle state and clear its cause; callers own the provider operation."""
    from connections.connected_accounts import ACCOUNT_STATES, ConnectedAccountError

    if state not in ACCOUNT_STATES:
        raise ConnectedAccountError("invalid connected account state")
    with write_transaction(db, scope="connected_account_state") as conn:
        if conn.execute(
            "UPDATE connected_accounts SET state=?,state_cause=NULL,updated_at=? WHERE id=?",
            (state, utc_now_iso(), account_id),
        ).rowcount != 1:
            raise ConnectedAccountError("connected account not found")


def mark_signed_in(
    account_id: str, *, address: str, provider_subject: str | None, actor: Actor,
    account_kind: str | None = None, db: sqlite3.Connection | None = None,
) -> None:
    """Record a completed sign-in on its own row: connected, by whom, as which address; any cause clears."""
    from connections.connected_accounts import _check_actor, _check_identity, get_account, provider_of

    _check_actor(actor)
    with write_transaction(db, scope="connected_account_signed_in") as conn:
        account = get_account(account_id, db=conn)
        _check_identity(provider_of(account["provider"]), provider_subject or account["provider_subject"],
                        account_kind or account["account_kind"])
        conn.execute(
            "UPDATE connected_accounts SET state='connected',state_cause=NULL,address=?,signed_in_by_kind=?,"
            "signed_in_by_id=?,provider_subject=COALESCE(provider_subject,?),account_kind=COALESCE(?,account_kind),"
            "updated_at=? WHERE id=?",
            (address.strip(), actor[0], actor[1], provider_subject, account_kind, utc_now_iso(), account_id),
        )


def mark_needs_sign_in(account_id: str, refresh_token: str | None = None, *, db: sqlite3.Connection | None = None) -> bool:
    """Mark one connected account needs-sign-in because the provider refused its saved sign-in,
    only if the refused refresh token still matches it."""
    from connections.connected_accounts import get_account, resolve_credentials

    provider: str | None = None
    with write_transaction(db, scope="connected_account_needs_sign_in") as conn:
        if refresh_token is not None:
            current = resolve_credentials(account_id, db=conn)
            held = current.get("refresh_token") if current is not None else None
            if not isinstance(held, str) or not secrets.compare_digest(held, refresh_token):
                return False
        changed = conn.execute(
            "UPDATE connected_accounts SET state='needs_sign_in',state_cause=?,updated_at=? "
            "WHERE id=? AND state='connected'",
            (REFRESH_TOKEN_INVALID, utc_now_iso(), account_id),
        ).rowcount == 1
        provider = str(get_account(account_id, db=conn)["provider"]) if changed else None
    if provider is not None:
        emit_lifecycle_event("connect_failed", account_id, provider, cause=REFRESH_TOKEN_INVALID)
    return provider is not None


def mark_sign_in_failed(account_id: str, cause: str, *, db: sqlite3.Connection | None = None) -> bool:
    """Record why signing this row in again failed: it needs sign-in, showing ``cause``.

    A connected row whose credential still works is left alone. False when the row is gone.
    """
    from connections.connected_accounts import ConnectedAccountError, get_account, is_connected, resolve_credentials

    if cause not in STATE_CAUSES:
        raise ConnectedAccountError("invalid account state cause")
    provider: str | None = None
    with write_transaction(db, scope="connected_account_sign_in_failed") as conn:
        if is_connected(account_id, db=conn) and resolve_credentials(account_id, db=conn) is not None:
            return False
        changed = conn.execute(
            "UPDATE connected_accounts SET state='needs_sign_in',state_cause=?,updated_at=? WHERE id=?",
            (cause, utc_now_iso(), account_id),
        ).rowcount == 1
        provider = str(get_account(account_id, db=conn)["provider"]) if changed else None
    if provider is not None:
        emit_lifecycle_event("connect_failed", account_id, provider, cause=cause)
    return provider is not None


def set_cursor(account_id: str, cursor: dict[str, object] | None, *, db: sqlite3.Connection | None = None) -> None:
    """Persist one provider cursor on its own account row."""
    from connections.connected_accounts import ConnectedAccountError

    encoded = json.dumps(cursor, separators=(",", ":"), sort_keys=True) if cursor is not None else None
    with write_transaction(db, scope="connected_account_cursor") as conn:
        if conn.execute(
            "UPDATE connected_accounts SET cursor_json=?,updated_at=? WHERE id=?", (encoded, utc_now_iso(), account_id),
        ).rowcount != 1:
            raise ConnectedAccountError("connected account not found")


def record_audit(
    account_id: str, action: str, actor: Actor, *, revocation_cause: str | None = None,
    db: sqlite3.Connection | None = None,
) -> int:
    """Record a fixed account lifecycle action without provider response text; returns its id."""
    from connections.connected_accounts import ConnectedAccountError

    if action not in AUDIT_ACTIONS or actor[0] not in {"person", "agent", "system"} or not actor[1]:
        raise ConnectedAccountError("invalid account audit")
    if revocation_cause is not None and revocation_cause not in REVOCATION_CAUSES:
        raise ConnectedAccountError("invalid account audit")
    with write_transaction(db, scope="connected_account_audit") as conn:
        if conn.execute("SELECT 1 FROM connected_accounts WHERE id=?", (account_id,)).fetchone() is None:
            raise ConnectedAccountError("connected account not found")
        inserted = conn.execute(
            "INSERT INTO connected_account_audit(created_at,account_id,actor_kind,actor_id,action,revocation_cause) "
            "VALUES (?,?,?,?,?,?)",
            (utc_now_iso(), account_id, actor[0], actor[1], action, revocation_cause),
        )
        return int(inserted.lastrowid or 0)


def set_audit_cause(audit_id: int, revocation_cause: str, *, db: sqlite3.Connection | None = None) -> None:
    """Fill in the fixed revocation outcome once the provider call after the audit settles."""
    from connections.connected_accounts import ConnectedAccountError

    if revocation_cause not in REVOCATION_CAUSES:
        raise ConnectedAccountError("invalid account audit")
    with write_transaction(db, scope="connected_account_audit_cause") as conn:
        conn.execute(
            "UPDATE connected_account_audit SET revocation_cause=? WHERE id=? AND revocation_cause IS NULL",
            (revocation_cause, audit_id),
        )
