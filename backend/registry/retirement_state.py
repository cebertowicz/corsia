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


def predecessors(artifact_id: str, *, db) -> list[str]:
    """Every identity whose successor chain ends at ``artifact_id``, in no promised order.

    The comments a document's earlier identities kept are part of it, so a read of the document
    asks for them, however many times it was promoted. One indexed lookup per identity found
    (``idx_artifact_identities_successor``). The recursion is a ``UNION``, so an id it has already
    reached adds no row and a loop of successors ends; ``artifact_id`` itself is never in the answer.
    """
    rows = db.execute(
        "WITH RECURSIVE lineage(id) AS ("
        " SELECT ?"
        " UNION"
        " SELECT i.id FROM lineage"
        " JOIN artifact_identities i"
        "   ON json_valid(i.metadata) AND json_extract(i.metadata, '$.successor_id') = lineage.id"
        " WHERE i.state != 'active')"
        " SELECT id FROM lineage WHERE id != ?",
        (artifact_id, artifact_id),
    ).fetchall()
    return [row["id"] for row in rows]


def live_identity(artifact_id: str, *, db) -> str | None:
    """The identity this document's comment state lives on now.

    ``artifact_id`` itself unless a promote retired it with a successor; then the active identity
    the successor chain ends at. None when the chain cannot be followed to one (a successor that is
    gone or retired with no successor of its own, or a loop): the caller must refuse rather than
    write to a retired row. The walk has no hop limit; an id it has already visited ends it, so a
    document promoted any number of times still resolves. An identity retired with no successor
    (its file deleted) answers itself, as it always did.
    """
    seen, current = {artifact_id}, artifact_id
    while True:
        successor = successor_id(current, db=db)
        if successor is None:
            return current if current == artifact_id or identity_state(current, db=db) == "active" else None
        if successor in seen:
            return None
        seen.add(successor)
        current = successor
