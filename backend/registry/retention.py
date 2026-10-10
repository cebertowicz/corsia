"""Identity-level artifact retention policy."""

from __future__ import annotations

from typing import Any

RETENTION_EPHEMERAL = "ephemeral"
RETENTION_DURABLE = "durable"


class RetentionChangeDenied(ValueError):
    """Raised when a caller cannot change an identity-level retention policy."""


def authorize_and_promote_explicit_durable_retention(
    db,
    *,
    identity: Any,
    inserted: bool,
    owner: Any,
    parent_thread_id: str,
    released: bool,
    retention: str | None,
    reject_retention_change: bool,
    now: str,
) -> None:
    """Apply an authorized durable request in the registration transaction."""
    if (
        inserted
        or retention != RETENTION_DURABLE
        or identity["retention"] != RETENTION_EPHEMERAL
    ):
        return
    denial = None
    if released:
        denial = (
            "retention cannot be changed after ownership is released; "
            "rehome the artifact to an owner first"
        )
    elif owner is not None and owner["parent_thread_id"] != parent_thread_id:
        denial = "retention can only be changed by the active owner"
    if denial is not None:
        if reject_retention_change:
            raise RetentionChangeDenied(denial)
        return
    db.execute(
        """UPDATE artifact_identities
           SET retention=?, updated_at=?
           WHERE id=? AND retention=?""",
        (RETENTION_DURABLE, now, identity["id"], RETENTION_EPHEMERAL),
    )
