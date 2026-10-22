"""Plans-owned lifecycle for trusted document-review requests."""
from __future__ import annotations

from infra.write_effects import after_commit
from plans import boundary_after, closure, notifications, review_evidence, service
from plans.plan_write import transaction
from plans.review_request_framing import REVIEW_PROVENANCE, STOPPED_DELIVERY_NOTE, note_block


def _notify(db, plan_id: str) -> None:
    after_commit(db, lambda: notifications.notify_after_commit((plan_id,), db=db))


def append_review_note(db, node_id: str, request) -> None:
    """Append the fixed provenance label without changing the review's scope."""
    with transaction(db) as connection:
        node_id = _current(connection, node_id)
        node = service._node_row(connection, node_id)
        text = f"{node['description']}\n\n{note_block(request)}"
        service.touch_node(connection, node_id, description=text)
        service._event(connection, node_id, "review_request_note_appended", actor_id=None,
                       provenance=REVIEW_PROVENANCE, payload={"request_id": request["id"]})
        _notify(connection, node["plan_id"])


def settle_review_node(db, node_id: str, *, evidence: str, provenance: str) -> None:
    """Record deterministic delivery evidence and legally accept once, atomically."""
    with transaction(db) as connection:
        settle_node(connection, node_id, evidence=evidence, provenance=provenance,
                    verified_by="system:document.review_request",
                    stopped_note=STOPPED_DELIVERY_NOTE, stopped_event="review_delivered_while_stopped")


def settle_node(connection, node_id: str, *, evidence: str, provenance: str, verified_by: str,
                stopped_note: str, stopped_event: str) -> None:
    """Record evidence on a delivery's task and accept it once, inside the caller's transaction.

    It settles the task's latest revision. A finished task stays as it is. A stopped one
    gets ``stopped_note`` (formatted with the evidence) and a ``stopped_event`` instead,
    once per distinct evidence, and never raises.
    """
    node_id = _current(connection, node_id)
    node = service._node_row(connection, node_id)
    if node["state"] in service._TERMINAL_STATES:
        return
    if review_evidence.stop_reason(connection, node) is not None:
        # A paused plan or a held step stays as the user left it, so nothing
        # settles it now or later: its owner finishes it after resume (pause
        # contract S26). The note replaces the framing's "settles by itself"
        # promise. Raising here would roll back every other document's delivery.
        # The same evidence again (a comment resolved twice) adds nothing.
        if connection.execute(
            "SELECT 1 FROM plan_node_events WHERE node_id=? AND event=? "
            "AND json_extract(payload_json,'$.evidence')=? LIMIT 1",
            (node_id, stopped_event, evidence),
        ).fetchone():
            return
        text = f"{node['description']}\n\n{stopped_note.format(evidence=evidence)}"
        service.touch_node(connection, node_id, description=text)
        service._event(connection, node_id, stopped_event, actor_id=None,
                       provenance=provenance, payload={"evidence": evidence})
        _notify(connection, node["plan_id"])
        return
    if node["state"] == "pending":
        # A rejection's revision starts pending; comments delivered before the
        # reviewer picks it up still answer the review. Activate, submit and
        # accept must stay in this one transaction: the active state never
        # commits on its own, and a refused submit rolls the activation back.
        # Admission is deliberately not used: selecting the revision would push
        # out the reviewer's current task only to settle it at once. Structured
        # Plans approved this attempt-less pending -> active step for review
        # settlement (corsia#79 at ec416332) and for feedback-batch settlement
        # (cebertowicz/corsia#1249 at 34a64f69c).
        service._transition(connection, node_id, "active", actor_id=None,
                            provenance=provenance, verification_result=None, verified_by=None)
    review_evidence.submit(node_id, evidence=evidence, expected_revision=None,
                           actor_id=None, provenance=provenance, db=connection)
    service._transition(connection, node_id, "accepted", actor_id=None,
                        provenance=provenance, verification_result="passed",
                        verified_by=verified_by)
    boundary_after.after_transition(node_id, "accepted", {
        "db": connection, "actor_id": None, "provenance": provenance,
    })
    _notify(connection, node["plan_id"])


def _current(db, node_id: str) -> str:
    """The review's latest revision.

    A rejection hands the reviewer a revision (a new node linked through
    ``follow_up_of`` with a ``revision_created`` event), so a review is the
    chain of revisions, and every review effect applies to its latest one.
    An archived revision is skipped, so the chain stops at the rejected node:
    the review then reads as ended at the rejection, and settle and close
    become no-ops.
    """
    from plans.rejection import _revision_of

    current = node_id
    while (revision := _revision_of(db, current)) is not None:
        current = revision[0].id
    return current


def review_is_open(db, node_id: str) -> bool:
    """Whether the review's latest revision is still unfinished.

    The one answer to "is this review still open"; request rows never store it.
    """
    return service._node_row(db, _current(db, node_id))["state"] not in service._TERMINAL_STATES


def close_review_node(
    db, node_id: str, *, reason: str, provenance: str = REVIEW_PROVENANCE,
) -> None:
    """Close through the ordinary Plans guards, releasing and reconciling its slot."""
    with transaction(db) as connection:
        node_id = _current(connection, node_id)
        node = service._node_row(connection, node_id)
        if node["state"] in service._TERMINAL_STATES:
            return
        closure.close_node(node_id, reason=reason, actor_id=None,
                           provenance=provenance, db=connection)
        boundary_after.after_transition(node_id, "closed", {
            "db": connection, "actor_id": None, "provenance": provenance,
        })
        _notify(connection, node["plan_id"])
