"""Administering account tokens after they are issued.

Listing and revoking tokens, and the lifecycle shell's service credential. Issuing and resolving a token stay in ``infra.accounts``,
which this module builds on and which does not import it.
"""

from __future__ import annotations

import sqlite3

from infra.accounts import _db, issue_token, token_digest
from infra.database import write_transaction


def lifecycle_token_label(operation_id: str) -> str:
    """Build the server-owned label for one lifecycle operation."""
    return f"lifecycle:{operation_id}"


def issue_service_token(user_id: str, label: str) -> str:
    """Mint one short-lived lifecycle credential for an explicit account."""
    token, _expires_at = issue_token(user_id, kind="service", label=label)
    return token


def issue_lifecycle_service_token(user_id: str, operation_id: str) -> str:
    """Mint the one-hour drain credential for one server-created operation."""
    return issue_service_token(user_id, lifecycle_token_label(operation_id))


def revoke_lifecycle_service_token(
    user_id: str, operation_id: str, *, db: sqlite3.Connection | None = None
) -> int:
    """Revoke the service credential attached to one settled lifecycle operation."""
    return revoke_label(user_id, lifecycle_token_label(operation_id), db=db)


def revoke_token(
    token: str, *, db: sqlite3.Connection | None = None
) -> bool:
    """Delete exactly this token. True iff it existed."""
    with write_transaction(db, scope="revoke_token") as conn:
        cursor = conn.execute(
            "DELETE FROM auth_tokens WHERE token_hash = ?", (token_digest(token),)
        )
        return bool(cursor.rowcount)


def revoke_label(
    user_id: str, label: str, *, db: sqlite3.Connection | None = None
) -> int:
    """Delete ``user_id``'s tokens carrying ``label``. Returns the count."""
    with write_transaction(db, scope="revoke_label") as conn:
        cursor = conn.execute(
            "DELETE FROM auth_tokens WHERE user_id = ? AND label = ?",
            (user_id, label),
        )
        return cursor.rowcount or 0


def list_tokens(
    user_id: str | None = None, db: sqlite3.Connection | None = None
) -> list[dict]:
    """List tokens (never their values) for one user, or for everyone."""
    sql = (
        "SELECT user_id, kind, label, created_at, expires_at FROM auth_tokens"
    )
    params: tuple = ()
    if user_id:
        sql += " WHERE user_id = ?"
        params = (user_id,)
    sql += " ORDER BY created_at DESC"
    return [dict(row) for row in _db(db).execute(sql, params).fetchall()]
