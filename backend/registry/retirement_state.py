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

    The permanent read path for comment rows that already sit on a retired identity: promotes made
    before ``retire`` moved comments to the successor, and comments an old pane saved on a retired
    id before #2798. ``retire`` stays the only place a promote puts comments
    (``doceditor.comment_transfer``); nothing moves rows off an identity that is already retired.
    There is no such move because it would relocate people's comments on existing installs and
    needs its own design, including the case where both identities hold comments
    (``refuse_if_occupied``). There is no hop cap; the ``UNION`` below ends the walk.
    One indexed lookup per identity found
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


def lineage_counts(db) -> tuple[int, int]:
    """What the lineage read still serves, read-only: (retired identities with a successor that hold
    comments, held feedback batches not yet sent that sit on a retired identity).

    A diagnostic only; it gates nothing. The first is how many retired identities the lineage read
    still serves. The second is feedback that cannot close (its artifact is not active) and so never
    reaches its agent; zero means nothing to re-key.
    """
    stray_comments = db.execute(
        "SELECT COUNT(*) FROM artifact_identities i"
        " WHERE json_valid(i.metadata) AND json_extract(i.metadata, '$.successor_id') IS NOT NULL"
        " AND EXISTS (SELECT 1 FROM doc_comments c WHERE c.artifact_id = i.id)"
    ).fetchone()[0]
    stray_batches = db.execute(
        "SELECT COUNT(*) FROM doc_feedback_batches b JOIN artifact_identities i ON i.id = b.artifact_id"
        " WHERE b.state IN ('open', 'undelivered') AND i.state != 'active'"
    ).fetchone()[0]
    return stray_comments, stray_batches


def log_lineage_counts(logger) -> None:
    """Write the :func:`lineage_counts` line at startup; a failure to count never stops startup."""
    from infra.database import get_db

    db = get_db()
    try:
        comments, batches = lineage_counts(db)
        logger.info(
            "document lineage: retired_identities_with_comments={} held_feedback_batches_on_retired_identities={}",
            comments, batches,
        )
    except Exception as exc:  # noqa: BLE001 - a count must never block startup
        logger.warning("document lineage count failed: {}", exc)
    finally:
        db.close()


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
