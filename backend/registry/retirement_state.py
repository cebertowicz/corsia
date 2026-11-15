"""What an identity's retirement left on it: its state, the cause and the successor ``models.retire`` recorded."""

from __future__ import annotations


def identity_state(artifact_id: str, *, db) -> str | None:
    """The identity's own lifecycle state, whoever owns it; ``None`` only when no such identity exists."""
    row = db.execute("SELECT state FROM artifact_identities WHERE id=?", (artifact_id,)).fetchone()
    return row["state"] if row else None


def retired_cause(artifact_id: str, *, db) -> str | None:
    """The ``cause`` its latest ``retire()`` was given, or None while the identity is active."""
    row = db.execute(
        "SELECT CASE WHEN state != 'active' AND json_valid(metadata)"
        " THEN json_extract(metadata, '$.retired_cause') END AS cause"
        " FROM artifact_identities WHERE id=?",
        (artifact_id,),
    ).fetchone()
    return row["cause"] if row else None


def successor_id(artifact_id: str, *, db) -> str | None:
    """The identity ``retire()`` handed this document to, or None while the identity is active."""
    row = db.execute(
        "SELECT CASE WHEN state != 'active' AND json_valid(metadata)"
        " THEN json_extract(metadata, '$.successor_id') END AS successor"
        " FROM artifact_identities WHERE id=?",
        (artifact_id,),
    ).fetchone()
    return row["successor"] if row else None


_MAX_HOPS = 8


def live_identity(artifact_id: str, *, db) -> str | None:
    """The identity this document's comment state lives on now.

    ``artifact_id`` itself unless a promote retired it with a successor; then the active identity
    the successor chain ends at. None when the chain cannot be followed to one (a successor that is
    gone or retired with no successor of its own, a loop, or more hops than a document is promoted
    in practice): the caller must refuse rather than write to a retired row. An identity retired
    with no successor (its file deleted) answers itself, as it always did.
    """
    seen, current = {artifact_id}, artifact_id
    for _ in range(_MAX_HOPS + 1):
        successor = successor_id(current, db=db)
        if successor is None:
            return current if current == artifact_id or identity_state(current, db=db) == "active" else None
        if successor in seen:
            return None
        seen.add(successor)
        current = successor
    return None
