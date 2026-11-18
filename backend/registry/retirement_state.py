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


def retired_identity_counts(db) -> tuple[int, int]:
    """What the one-time move left behind, read-only: (retired identities with a successor that still
    hold comments, open feedback batches that sit on a retired identity).

    A diagnostic only; it gates nothing. After ``migration_199_to_200`` the first counts the identities
    that held a reply whose parent is outside the document's lineage, or whose chain ends nowhere.
    The second is feedback that cannot close (its artifact is not active) and so never reaches its agent.
    """
    comments = db.execute(
        "SELECT COUNT(*) FROM artifact_identities i"
        " WHERE json_valid(i.metadata) AND json_extract(i.metadata, '$.successor_id') IS NOT NULL"
        " AND EXISTS (SELECT 1 FROM doc_comments c WHERE c.artifact_id = i.id)"
    ).fetchone()[0]
    batches = db.execute(
        "SELECT COUNT(*) FROM doc_feedback_batches b JOIN artifact_identities i ON i.id = b.artifact_id"
        " WHERE b.state IN ('open', 'undelivered') AND i.state != 'active'"
    ).fetchone()[0]
    return comments, batches


def log_retired_identity_counts(logger) -> None:
    """Write the :func:`retired_identity_counts` line at startup; a failure to count never stops startup."""
    from infra.database import get_db

    db = get_db()
    try:
        comments, batches = retired_identity_counts(db)
        logger.info(
            "document identities: retired_identities_with_comments={} held_feedback_batches_on_retired_identities={}",
            comments, batches,
        )
    except Exception as exc:  # noqa: BLE001 - a count must never block startup
        logger.warning("retired identity count failed: {}", exc)
    finally:
        db.close()
