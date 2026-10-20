"""Canonical review-request service shared by the user, agent and CLI adapters."""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from core.agent_roster_queries import agent_rows
from doceditor import feedback_delivery
from doceditor.review_feedback import submit_fork_feedback, withdraw_fork_feedback
from doceditor.comments import validate_client_id
from infra.auth import is_owner_sentinel, may_access_artifact
from infra.corsia_agent import CorsiaAgentSettingError, get_corsia_agent
from infra.queries import get_thread_for_user
from infra.database import write_transaction
from infra.write_effects import after_commit
from plans import public
from registry import models
from registry.change_notifications import build_artifact_change_messages

OWNER_REFUSAL = "You own this document"
ROSTER_REFUSAL = "Reviewer {id} is not an active agent in your visible roster; use agent_roster for reviewer thread IDs."
USER_ROSTER_REFUSAL = "This reviewer is no longer in your visible roster."
NOTE_REFUSAL = "note must be at most 500 characters (got {length})."
CANCEL_REFUSAL = "Only the requester or document owner can cancel this review request"
ARTIFACT_REFUSAL = "Artifact not found"
THREAD_REFUSAL = "Thread not found"
SELF_REFUSAL = "You cannot request a review from yourself"
ROSTER_ERROR = "Could not load reviewers. Try again."
REVIEWER_REASON_COPY = {
    "owner_unaddressable": "Can't be reached right now",
    "owner_archived": "Archived",
    "owner_unavailable": "Not available right now",
    "owner_admission_refused": "Not accepting work right now",
}
DEFAULT_REVIEWER_UNAVAILABLE = "Not available right now"
REVIEWER_NOT_IN_ROSTER = "reviewer_not_in_roster"
DEFAULT_REQUEST_ERROR_CODE = "review_request_refused"
CANCEL_REASON = "Review request cancelled by its requester or document owner."
#: Outcome of a request whose review task ended before any batch reached the owner.
ENDED_WITHOUT_COMMENTS = "ended_without_comments"
IN_REVIEW_REFUSAL = "A review is already in progress; retry with the same client_id."


class ReviewRequestError(ValueError):
    """An explicit review refusal with no durable request or plan effect."""

    def __init__(self, message: str, code: str = DEFAULT_REQUEST_ERROR_CODE):
        super().__init__(message)
        self.code = code


class ReviewRosterError(Exception):
    """The visible roster could not be read; never expose database details."""


@dataclass(frozen=True)
class ReviewerEligibility:
    choices: list[dict]
    refusals: dict[str, str]


@dataclass(frozen=True)
class ReviewActor:
    kind: str
    id: str
    thread_id: str
    user_id: str
    display: str


def reviewer_reason_text(reason: str) -> str:
    """Return the single user-facing label for an unavailable reviewer."""
    return REVIEWER_REASON_COPY.get(reason, DEFAULT_REVIEWER_UNAVAILABLE)


def _artifact(db, artifact_id: str, actor: ReviewActor):
    artifact = models.get_artifact(artifact_id, db=db)
    if artifact is None or artifact["state"] != "active" or not may_access_artifact(artifact["parent_thread_id"], actor.user_id, db=db):
        raise PermissionError(ARTIFACT_REFUSAL)
    return artifact


def reviewer_eligibility(db, artifact, actor: ReviewActor, *,
                         reviewer_id: str | None = None) -> ReviewerEligibility:
    """One roster/eligibility rule for picker and submit, with scoped admission.

    The picker checks each row once. Submit checks only its named recipient,
    and calls again under the same write lock after the final Plans effect.
    """
    if get_thread_for_user(actor.thread_id, actor.user_id, db=db) is None:
        raise PermissionError(THREAD_REFUSAL)
    refusals = {artifact["parent_thread_id"]: OWNER_REFUSAL}
    if actor.kind == "agent":
        refusals.setdefault(actor.id, SELF_REFUSAL)
    try:
        _caller, visible = agent_rows(actor.thread_id, db)
        corsia_agent_id = get_corsia_agent(db=db)
    except (sqlite3.OperationalError, CorsiaAgentSettingError) as exc:
        raise ReviewRosterError(ROSTER_ERROR) from exc
    choices = []
    for row in visible:
        thread_id = row["id"]
        if thread_id in refusals or (reviewer_id is not None and thread_id != reviewer_id):
            continue
        # The roster lists the CORSIA agent so agents can message it; it is never a document reviewer.
        if thread_id == corsia_agent_id:
            continue
        problem = feedback_delivery.recipient_problem(db, thread_id)
        if problem is not None:
            # Named for submit's refusal, never offered: a reviewer who cannot take the work is not a choice.
            refusals[thread_id] = problem
            continue
        choices.append({
            "id": thread_id, "name": row["title"] or thread_id,
            "responsibility": row["agent_responsibility"],
            "unavailable_reason": problem,
            "unavailable_text": reviewer_reason_text(problem) if problem is not None else None,
        })
    return ReviewerEligibility(choices, refusals)


def _reviewer(db, artifact, reviewer: str, actor: ReviewActor) -> None:
    eligible = reviewer_eligibility(db, artifact, actor, reviewer_id=reviewer)
    if reviewer in eligible.refusals:
        reason = eligible.refusals[reviewer]
        code = DEFAULT_REQUEST_ERROR_CODE if reason in {OWNER_REFUSAL, SELF_REFUSAL} else reason
        raise ReviewRequestError(reason, code=code)
    if not eligible.choices:
        raise ReviewRequestError(ROSTER_REFUSAL.format(id=reviewer), code=REVIEWER_NOT_IN_ROSTER)


def _view(db, row) -> dict:
    """A stored request with its outcome, the only form callers receive."""
    return {**dict(row), "outcome": outcome(db, row)}


def _get(db, request_id: str) -> dict:
    return _view(db, db.execute("SELECT * FROM doc_review_requests WHERE id=?", (request_id,)).fetchone())


def _changed(db, request: dict, artifact) -> None:
    affected = [request["reviewer_thread_id"], artifact["parent_thread_id"]]
    if request["requester_kind"] == "agent":
        affected.append(request["requester_id"])
    messages = build_artifact_change_messages(request["artifact_id"], owner_thread_id=artifact["parent_thread_id"], affected_thread_ids=affected, db=db)
    after_commit(db, lambda: public.deliver_notifications(messages))


def _repeat(db, row, actor: ReviewActor, note: str | None, key: str) -> dict:
    keys = json.loads(row["client_ids"])
    if key in keys:
        return _view(db, row)
    keys.append(key)
    db.execute("UPDATE doc_review_requests SET client_ids=? WHERE id=?", (json.dumps(keys), row["id"]))
    if note:
        if row["node_id"] is None:
            # A fork has already frozen its instruction; silently accepting a
            # later note would claim the reviewer saw it when it could not.
            raise ReviewRequestError(IN_REVIEW_REFUSAL)
        public.append_review_note(db, row["node_id"], {
            **dict(row), "note": note, "requester_display": actor.display,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    return _get(db, row["id"])


def request_review(db, artifact_id: str, reviewer_thread_id: str, requester: ReviewActor,
                   note: str | None, client_id: str) -> dict:
    """Queue one reviewer task, with retry-safe notes and no preemption."""
    key = validate_client_id(client_id)
    if key is None:
        # Reuse the canonical validator's refusal for an empty key.
        validate_client_id("")
    if note is not None and len(note) > 500:
        raise ReviewRequestError(NOTE_REFUSAL.format(length=len(note)))
    with write_transaction(db, scope="request_document_review") as connection:
        artifact = _artifact(connection, artifact_id, requester)
        _reviewer(connection, artifact, reviewer_thread_id, requester)
        prior = connection.execute(
            """SELECT r.* FROM doc_review_requests r WHERE artifact_id=? AND reviewer_thread_id=?
               AND EXISTS(SELECT 1 FROM json_each(r.client_ids) WHERE value=?)
               ORDER BY created_at LIMIT 1""", (artifact_id, reviewer_thread_id, key),
        ).fetchone()
        if prior is not None:
            return _view(connection, prior)
        # At most one open request per reviewer and document. Openness comes from
        # Plans, so no index can express it; the write lock serializes this check
        # and the insert below.
        latest = _latest(connection, artifact_id, reviewer_thread_id)
        if latest is not None and outcome(connection, latest) == "open":
            return _repeat(connection, latest, requester, note, key)
        from doceditor.review_fork_intent import requesting_node_id

        request_id = str(uuid.uuid4())
        intent_node = requesting_node_id(connection, requester)
        request = {
            "id": request_id, "artifact_id": artifact_id,
            "requester_kind": requester.kind, "requester_id": requester.id,
            "reviewer_thread_id": reviewer_thread_id, "note": note,
            "fork_id": str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(), "requester_display": requester.display,
        }
        connection.execute(
            """INSERT INTO doc_review_requests(id,artifact_id,requester_kind,requester_id,
               reviewer_thread_id,note,client_ids,fork_id,requesting_node_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (request["id"], artifact_id, requester.kind, requester.id, reviewer_thread_id,
             note, json.dumps([key]), request["fork_id"], intent_node, request["created_at"]),
        )
        # Recheck admission under the same write lock. A request is fork-first:
        # Plans is reserved for terminal fallback only.
        _reviewer(connection, artifact, reviewer_thread_id, requester)
        result = _get(connection, request_id)
        _changed(connection, result, artifact)
        return result


def outcome(db, row) -> str:
    """``cancelled``, ``commented``, ``open`` or ``ended_without_comments``.

    The row stores only its own events (cancelled, answered); whether the review
    is still open comes from Plans alone.
    """
    if row["cancelled_at"]:
        return "cancelled"
    if row["answered_batch_id"]:
        return "commented"
    if row["node_id"] is None:
        return "open" if row["fork_id"] else ENDED_WITHOUT_COMMENTS
    return "open" if public.review_is_open(db, row["node_id"]) else ENDED_WITHOUT_COMMENTS


def for_document(db, artifact_id: str) -> list[dict]:
    """Each reviewer's latest request, while it is open or ended without comments."""
    rows = db.execute(
        "SELECT * FROM doc_review_requests r WHERE artifact_id=?"
        " AND cancelled_at IS NULL AND answered_batch_id IS NULL"
        " AND NOT EXISTS (SELECT 1 FROM doc_review_requests later WHERE later.artifact_id=r.artifact_id"
        " AND later.reviewer_thread_id=r.reviewer_thread_id AND later.created_at>r.created_at)"
        " ORDER BY created_at,id", (artifact_id,))
    return [_view(db, row) for row in rows]


def may_cancel(db, artifact, row, actor: ReviewActor) -> bool:
    """The requester or real thread owner may cancel; shared access is not ownership."""
    if row["requester_kind"] == actor.kind and row["requester_id"] == actor.id:
        return True
    owner_id = artifact["parent_thread_id"]
    if is_owner_sentinel(owner_id):
        return False
    if actor.kind == "agent":
        return owner_id == actor.thread_id
    return get_thread_for_user(owner_id, actor.user_id, db=db) is not None


def escalate_fork(db, request_id: str) -> dict | None:
    """Turn an unfinished fork into the established reviewer node exactly once."""
    with write_transaction(db, scope="escalate_document_review_fork") as connection:
        row = connection.execute("SELECT * FROM doc_review_requests WHERE id=?", (request_id,)).fetchone()
        if (row is None or row["cancelled_at"] or row["answered_batch_id"]
                or row["node_id"] is not None or not row["fork_id"]):
            return None
        requester = connection.execute("SELECT title FROM threads WHERE id=?", (row["requester_id"],)).fetchone()
        comments = connection.execute(
            "SELECT DISTINCT c.id FROM doc_comments c "
            "JOIN doc_feedback_batch_items i ON i.comment_id=c.id "
            "JOIN doc_feedback_batches b ON b.id=i.batch_id "
            "WHERE b.review_request_id=? AND b.state IN ('open','undelivered') ORDER BY c.id",
            (request_id,),
        ).fetchall()
        continuation = "Continue the requested review; comments already staged: " + (
            ", ".join(str(comment["id"]) for comment in comments) or "none"
        )
        if row["requesting_node_id"]:
            continuation += f". Requesting intent node: {row['requesting_node_id']}."
        request = {
            **dict(row), "intent_node_id": row["requesting_node_id"],
            "requester_display": (requester["title"] if requester else None) or row["requester_id"],
            "note": f"{row['note'] or ''}\n\n{continuation}".strip(),
        }
        node = public.create_review_node(connection, row["reviewer_thread_id"], request)
        connection.execute(
            "UPDATE doc_review_requests SET node_id=?,fork_id=NULL WHERE id=?",
            (node.node_id, request_id),
        )
        # Staged comments wait for the reviewer's own continuation: a request
        # batch's closes_at is now the earliest reviewer Turn start that may
        # close it (feedback_batches.close_for_turn_end); no quiet timer does.
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            "UPDATE doc_feedback_batches SET closes_at=?,updated_at=? WHERE review_request_id=? AND state='open'",
            (now, now, request_id),
        )
        artifact = models.get_artifact(row["artifact_id"], db=connection)
        if artifact is not None:
            _changed(connection, request, artifact)
        after_commit(connection, lambda: public.notify_after_commit((public.get_node(node.node_id, db=connection).plan_id,), db=connection))
        return _get(connection, request_id)


def cancel(db, request_id: str, actor: ReviewActor) -> dict:
    with write_transaction(db, scope="cancel_document_review") as connection:
        row = connection.execute("SELECT * FROM doc_review_requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            raise PermissionError(ARTIFACT_REFUSAL)
        artifact = _artifact(connection, row["artifact_id"], actor)
        if not may_cancel(connection, artifact, row, actor):
            raise PermissionError(CANCEL_REFUSAL)
        if outcome(connection, row) != "open":
            return _view(connection, row)
        connection.execute("UPDATE doc_review_requests SET cancelled_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), request_id))
        if row["node_id"] is not None:
            public.close_review_node(connection, row["node_id"], reason=CANCEL_REASON)
        else:
            from core.artifact_review_runtime import cancel as cancel_fork

            after_commit(connection, lambda: cancel_fork(str(row["fork_id"])))
        withdraw_fork_feedback(connection, request_id)
        result = _get(connection, request_id)
        _changed(connection, result, artifact)
        return result


def settle_for_batch(db, batch) -> tuple[str, ...]:
    """Answer the reviewer's latest request with an agent's delivered owner batch.

    Runs in the delivery transaction. The batch answers that request when it is
    neither cancelled nor already answered, even if the review task has ended,
    so late comments still read as commented; the review task is accepted only
    while it is open.
    """
    if batch["author_kind"] != "agent":
        return ()
    delivered = db.execute("SELECT state,delivery_outcome FROM doc_feedback_batches WHERE id=?", (batch["id"],)).fetchone()
    if delivered is None or delivered["state"] != "delivered" or delivered["delivery_outcome"] not in {"node", "ui"}:
        return ()
    artifact = models.get_artifact(batch["artifact_id"], db=db)
    if artifact is None or batch["recipient_id"] != artifact["parent_thread_id"]:
        return ()
    request_id = batch["review_request_id"]
    row = (db.execute("SELECT * FROM doc_review_requests WHERE id=?", (request_id,)).fetchone()
           if request_id else _latest(db, batch["artifact_id"], batch["author_id"]))
    if row is None or row["cancelled_at"] or row["answered_batch_id"]:
        return ()
    if not request_id and row["fork_id"] and row["node_id"] is None:
        return ()
    node_id = row["node_id"]
    still_open = node_id is not None and public.review_is_open(db, node_id)
    db.execute("UPDATE doc_review_requests SET answered_batch_id=? WHERE id=?", (batch["id"], row["id"]))
    woken: list[str] = []
    if still_open:
        with public.collect_wake_recipients(db, woken):
            public.settle_review_node(db, node_id, evidence=f"Document review comments delivered to the owner in batch {batch['id']}.", provenance="document.review_request")
    _changed(db, dict(row), artifact)
    return tuple(woken)


def _latest(db, artifact_id: str, reviewer_thread_id: str):
    """The reviewer's latest request on this document, whatever its outcome."""
    return db.execute(
        "SELECT * FROM doc_review_requests WHERE artifact_id=? AND reviewer_thread_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
        (artifact_id, reviewer_thread_id)).fetchone()
