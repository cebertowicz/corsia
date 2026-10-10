"""Read paths over durable host-access lease state.

``infra.host_access_leases`` owns lease truth and re-exports these reads, so
callers keep importing them from there. The lease module's private helpers are
imported inside each read: that module imports this one after its own
definitions, and a top-level import back would form a cycle.
"""

from __future__ import annotations

from datetime import datetime
import sqlite3
from typing import Any

from infra.database import get_db


def list_leases(
    user_id: str,
    *,
    root_thread_id: str | None = None,
    nonterminal_only: bool = False,
    now: datetime | None = None,
    db: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """List one user's leases after applying authoritative expiry.

    The common read takes no writer lock: a write transaction opens only when
    an active lease is already due to expire.
    """
    from infra.host_access_leases import (
        _SELECT,
        NONTERMINAL_STATES,
        LeaseConflictError,
        _expire_due,
        _now,
        _public_row,
        _transaction,
    )

    timestamp = _now(now)
    connection = db if db is not None else get_db()
    owns_connection = db is None
    try:
        if connection.in_transaction:
            raise LeaseConflictError(
                "host-access reads require a clean transaction boundary"
            )
        due = connection.execute(
            "SELECT 1 FROM host_access_leases "
            "WHERE state='active' AND expires_at IS NOT NULL AND expires_at<=? "
            "LIMIT 1",
            (timestamp.isoformat(),),
        ).fetchone()
        if due:
            with _transaction(db) as writer:
                _expire_due(writer, timestamp)
        filters = ["l.user_id=?"]
        params: list[Any] = [user_id]
        if root_thread_id is not None:
            filters.append("l.root_thread_id=?")
            params.append(root_thread_id)
        if nonterminal_only:
            state_values = tuple(sorted(NONTERMINAL_STATES))
            placeholders = ",".join("?" for _ in state_values)
            filters.append(f"l.state IN ({placeholders})")
            params.extend(state_values)
        rows = connection.execute(
            _SELECT
            + "WHERE "
            + " AND ".join(filters)
            + " ORDER BY l.created_at DESC,l.id DESC",
            params,
        ).fetchall()
        return [_public_row(row) for row in rows]
    finally:
        if owns_connection:
            connection.close()
