"""Operation-scoped authentication for detached scheduled restart callbacks."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from infra.database import get_db


def mint_callback_credential() -> str:
    """Create an unpersisted secret for one scheduled lifecycle shell."""
    return secrets.token_urlsafe(32)


def callback_credential_hash(credential: str) -> str:
    """Hash a callback credential before it crosses the durable boundary."""
    return hashlib.sha256(credential.encode()).hexdigest()


def callback_credential_matches(operation_id: str, credential: str, *, db=None) -> bool:
    """Accept a credential only for its live operation's lifecycle callbacks."""
    if not credential:
        return False
    connection = db or get_db()
    try:
        row = connection.execute(
            "SELECT callback_credential_hash FROM self_restart_requests "
            "WHERE id=? AND status='in_flight'",
            (operation_id,),
        ).fetchone()
        stored = row["callback_credential_hash"] if row is not None else None
        return isinstance(stored, str) and hmac.compare_digest(
            stored, callback_credential_hash(credential)
        )
    finally:
        if db is None:
            connection.close()
