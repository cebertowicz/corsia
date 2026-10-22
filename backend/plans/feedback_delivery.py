"""Plan-owned state queries and settlement for document-feedback delivery."""

from __future__ import annotations

from plans import boundary_after, review_evidence, review_requests, service
from plans.plan_write import transaction

PROVENANCE = "document.feedback_batch"
STOPPED_NOTE = "{evidence} This step was stopped then, so it did not settle by itself; mark it done once it resumes."


def _existing(db, recipient_thread_id: str, batch_id: str, provenance: str = PROVENANCE) -> str | None:
    row = db.execute(
        """SELECT l.node_id FROM plan_node_artifact_links l
           JOIN plan_nodes n ON n.id=l.node_id
           JOIN plans p ON p.id=n.plan_id
           WHERE p.owner_kind='thread' AND p.owner_id=?
             AND l.provenance=? AND l.source_event_id=?
           ORDER BY l.created_at LIMIT 1""",
        (recipient_thread_id, provenance, batch_id),
    ).fetchone()
    return str(row["node_id"]) if row else None


def settle_feedback_node(db, recipient_thread_id: str, batch_id: str, *, evidence: str) -> None:
    """Settle an active feedback task; leave a stopped task in its stopped state with a note."""
    node_id = _existing(db, recipient_thread_id, batch_id)
    if node_id is None:
        return
    with transaction(db) as connection:
        node = service._node_row(connection, node_id)
        if node["state"] in service._TERMINAL_STATES:
            return
        if review_evidence.stop_reason(connection, node) is not None:
            service.touch_node(
                connection,
                node_id,
                description=f"{node['description']}\n\n{STOPPED_NOTE.format(evidence=evidence)}",
            )
            service._event(
                connection,
                node_id,
                "feedback_settled_while_stopped",
                actor_id=None,
                provenance=PROVENANCE,
                payload={"evidence": evidence},
            )
            review_requests._notify(connection, node["plan_id"])
            return
        if node["state"] == "pending":
            service._transition(connection, node_id, "active", actor_id=None, provenance=PROVENANCE,
                                verification_result=None, verified_by=None)
        review_evidence.submit(node_id, evidence=evidence, expected_revision=None,
                               actor_id=None, provenance=PROVENANCE, db=connection)
        service._transition(connection, node_id, "accepted", actor_id=None, provenance=PROVENANCE,
                            verification_result="passed", verified_by="system:document.feedback_batch")
        boundary_after.after_transition(node_id, "accepted", {
            "db": connection, "actor_id": None, "provenance": PROVENANCE,
        })
        review_requests._notify(connection, node["plan_id"])


def feedback_node_has_started(db, recipient_thread_id: str, node_id: str) -> bool:
    """Whether the recipient has ever started a turn for this feedback task."""
    return db.execute(
        "SELECT 1 FROM active_turns WHERE thread_id=? AND plan_node_id=?",
        (recipient_thread_id, node_id),
    ).fetchone() is not None


def recipient_turn_is_active(db, recipient_thread_id: str) -> bool:
    """Whether the recipient is already consuming a running or retrying turn."""
    return db.execute(
        """SELECT 1 FROM active_turns WHERE thread_id=?
           AND status IN ('running','waiting_retry')""",
        (recipient_thread_id,),
    ).fetchone() is not None


def unstarted_feedback_batch_id(
    db,
    *,
    recipient_thread_id: str,
    author_kind: str,
    author_id: str,
    artifact_id: str,
) -> str | None:
    """Return the newest delivered batch whose recipient has never started its task."""
    rows = db.execute(
        """SELECT batch.id,node.id AS node_id FROM doc_feedback_batches batch
           JOIN plan_nodes node ON node.id=batch.delivered_node_id AND node.state='active'
           JOIN plans plan ON plan.id=node.plan_id
           WHERE batch.author_kind=? AND batch.author_id=? AND batch.artifact_id=?
             AND batch.recipient_id=? AND batch.state='delivered'
             AND batch.delivery_outcome='node'
             AND plan.owner_kind='thread' AND plan.owner_id=?
           ORDER BY batch.updated_at DESC,batch.id DESC""",
        (author_kind, author_id, artifact_id, recipient_thread_id, recipient_thread_id),
    ).fetchall()
    for row in rows:
        if not feedback_node_has_started(db, recipient_thread_id, str(row["node_id"])):
            return str(row["id"])
    return None
