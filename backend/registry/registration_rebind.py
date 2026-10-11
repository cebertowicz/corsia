"""Rebind lookup for artifact registration owners."""

from __future__ import annotations

from registry.identity_bridge import OriginRebind


def settled_row_rebind(
    db, artifact_id: str, thread_id: str | None,
) -> OriginRebind | None:
    """Return an unbound rebind when the thread has a settled registration."""
    if thread_id is None:
        return None
    settled = db.execute(
        "SELECT 1 FROM artifact_registrations "
        "WHERE artifact_id=? AND parent_thread_id=? AND surfacing='settled'",
        (artifact_id, thread_id),
    ).fetchone()
    return OriginRebind(None) if settled else None
