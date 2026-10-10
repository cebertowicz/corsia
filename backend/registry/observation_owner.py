"""Who may register an observed write to an artifact."""

from __future__ import annotations

from registry import models


def require_observable_owner(db, thread_id: str, external_id: str, existing) -> None:
    """Reject observations whose caller or surviving owner cannot be verified."""
    caller = db.execute(
        "SELECT user_id FROM threads WHERE id=?", (thread_id,)
    ).fetchone()
    if caller is None or not caller["user_id"]:
        raise PermissionError(
            "observed artifact registration requires a valid caller thread"
        )
    if existing is None:
        return
    owner_thread_id = existing.get("parent_thread_id")
    if not owner_thread_id:
        if models.latest_released_owner_thread_id(existing["id"], db=db) is None:
            raise PermissionError(f"{external_id} has no verifiable owner thread")
        return
    owner = db.execute(
        "SELECT user_id FROM threads WHERE id=?", (owner_thread_id,)
    ).fetchone()
    if owner is None or not owner["user_id"]:
        raise PermissionError(f"{external_id} has no verifiable owner thread")
    if owner_thread_id != thread_id and owner["user_id"] != caller["user_id"]:
        raise PermissionError(
            f"{external_id} is already owned by another user's thread"
        )
