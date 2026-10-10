"""Canonical review-request service shared by the user, agent and CLI adapters."""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from core.agent_roster_queries import agent_rows
from doceditor import feedback_delivery
from doceditor.comments import validate_client_id
from infra.auth import is_owner_sentinel, may_access_artifact
from infra.conductor import ConductorSettingError, get_conductor
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
        workspace_conductor = get_conductor(db=db)
    except (sqlite3.OperationalError, ConductorSettingError) as exc:
        raise ReviewRosterError(ROSTER_ERROR) from exc
    choices = []
    for row in visible:
        thread_id = row["id"]
        if thread_id in refusals or (reviewer_id is not None and thread_id != reviewer_id):
            continue
        # The roster lists the workspace conductor so agents can message it; it is never a document reviewer.
        if thread_id == workspace_conductor:
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
        request_id = str(uuid.uuid4())
        request = {
            "id": request_id, "artifact_id": artifact_id,
            "requester_kind": requester.kind, "requester_id": requester.id,
            "reviewer_thread_id": reviewer_thread_id, "note": note,
            "created_at": datetime.now(timezone.utc).isoformat(), "requester_display": requester.display,
        }
        connection.execute(
            """INSERT INTO doc_review_requests(id,artifact_id,requester_kind,requester_id,
               reviewer_thread_id,note,client_ids,created_at) VALUES (?,?,?,?,?,?,?,?)""",
            (request["id"], artifact_id, requester.kind, requester.id, reviewer_thread_id, note, json.dumps([key]), request["created_at"]),
        )
        node = public.create_review_node(connection, reviewer_thread_id, request)
        connection.execute("UPDATE doc_review_requests SET node_id=? WHERE id=?", (node.node_id, request["id"]))
        # Recheck under the same write lock after the final Plans effect.
        _reviewer(connection, artifact, reviewer_thread_id, requester)
        result = _get(connection, request_id)
        _changed(connection, result, artifact)
        after_commit(connection, lambda: public.notify_after_commit((public.get_node(node.node_id, db=connection).plan_id,), db=connection))
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
        public.close_review_node(connection, row["node_id"], reason=CANCEL_REASON)
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
    row = _latest(db, batch["artifact_id"], batch["author_id"])
    if row is None or row["cancelled_at"] or row["answered_batch_id"]:
        return ()
    still_open = public.review_is_open(db, row["node_id"])
    db.execute("UPDATE doc_review_requests SET answered_batch_id=? WHERE id=?", (batch["id"], row["id"]))
    woken: list[str] = []
    if still_open:
        with public.collect_wake_recipients(db, woken):
            public.settle_review_node(db, row["node_id"], evidence=f"Document review comments delivered to the owner in batch {batch['id']}.", provenance="document.review_request")
    _changed(db, dict(row), artifact)
    return tuple(woken)


def _latest(db, artifact_id: str, reviewer_thread_id: str):
    """The reviewer's latest request on this document, whatever its outcome."""
    return db.execute(
        "SELECT * FROM doc_review_requests WHERE artifact_id=? AND reviewer_thread_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
        (artifact_id, reviewer_thread_id)).fetchone()
