"""Canonical review-request service shared by the user, agent and CLI adapters."""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from core.agent_roster_queries import agent_rows
from doceditor import feedback_delivery
from doceditor.comments import validate_client_id
from doceditor.review_lifecycle import FALLBACK_STATES, is_open, notify_change, transition
from infra.auth import is_owner_sentinel, may_access_artifact
from infra.corsia_agent import CorsiaAgentSettingError, get_corsia_agent
from infra.database import write_transaction
from infra.queries import get_thread_for_user
from infra.write_effects import after_commit
from plans import public
from prompts.artifact_review import continuation
from registry import models

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
IN_REVIEW_REFUSAL = "A review is already in progress. Wait for it to finish, or cancel it and request a new review with your note."


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
    """Expose the stored lifecycle without a parallel status vocabulary."""
    return dict(row)


def get_request(db, request_id: str) -> dict:
    return _view(db, db.execute("SELECT * FROM doc_review_requests WHERE id=?", (request_id,)).fetchone())


def _repeat(db, row, actor: ReviewActor, note: str | None, key: str) -> dict:
    keys = json.loads(row["client_ids"])
    if key in keys:
        return _view(db, row)
    keys.append(key)
    db.execute("UPDATE doc_review_requests SET client_ids=? WHERE id=?", (json.dumps(keys), row["id"]))
    if note:
        if row["state"] == "forked":
            # A fork has already frozen its instruction; silently accepting a
            # later note would claim the reviewer saw it when it could not.
            raise ReviewRequestError(IN_REVIEW_REFUSAL)
        public.append_review_note(db, row["node_id"], {
            **dict(row), "note": note, "requester_display": actor.display,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    return get_request(db, row["id"])


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
        # The write lock serializes the pending request check and insert.
        latest = _latest(connection, artifact_id, reviewer_thread_id)
        if latest is not None and is_open(connection, latest):
            return _repeat(connection, latest, requester, note, key)
        request_id = str(uuid.uuid4())
        request = {
            "id": request_id, "artifact_id": artifact_id,
            "requester_kind": requester.kind, "requester_id": requester.id,
            "reviewer_thread_id": reviewer_thread_id, "note": note,
            "fork_id": str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(), "requester_display": requester.display,
        }
        connection.execute(
            """INSERT INTO doc_review_requests(id,artifact_id,requester_kind,requester_id,
               reviewer_thread_id,note,client_ids,fork_id,state,state_changed_at,created_at)
               VALUES (?,?,?,?,?,?,?,?,'forked',?,?)""",
            (request["id"], artifact_id, requester.kind, requester.id, reviewer_thread_id,
             note, json.dumps([key]), request["fork_id"], request["created_at"], request["created_at"]),
        )
        # Recheck admission under the same write lock. A request is fork-first:
        # Plans is reserved for terminal fallback only.
        _reviewer(connection, artifact, reviewer_thread_id, requester)
        result = get_request(connection, request_id)
        notify_change(connection, result, artifact)
        return result


def for_document(db, artifact_id: str) -> list[dict]:
    """Each reviewer's latest request, while it is open or ended without comments."""
    rows = db.execute(
        "SELECT * FROM doc_review_requests r WHERE artifact_id=?"
        " AND (state IN ('forked','handed_back','failed') OR (state='submitted' AND answered_batch_id IS NULL))"
        " AND NOT EXISTS (SELECT 1 FROM doc_review_requests later WHERE later.artifact_id=r.artifact_id"
        " AND later.reviewer_thread_id=r.reviewer_thread_id AND later.created_at>r.created_at)"
        " ORDER BY created_at,id", (artifact_id,))
    return [_view(db, row) for row in rows
            if row["state"] not in FALLBACK_STATES or is_open(db, row)]


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


def escalate_fork(db, request_id: str, *, reason: str = "limit",
                  state: Literal["handed_back", "failed"] | None = None) -> dict | None:
    """Turn an unfinished fork into the established reviewer node exactly once."""
    with write_transaction(db, scope="escalate_document_review_fork") as connection:
        row = connection.execute("SELECT * FROM doc_review_requests WHERE id=?", (request_id,)).fetchone()
        if row is None or row["state"] != "forked":
            return None
        requester = connection.execute("SELECT title FROM threads WHERE id=?", (row["requester_id"],)).fetchone()
        comments = connection.execute(
            "SELECT DISTINCT i.comment_id AS id FROM doc_feedback_batch_items i "
            "JOIN doc_feedback_batches b ON b.id=i.batch_id "
            "WHERE b.artifact_id=? AND b.author_kind='agent' AND b.author_id=? "
            "AND b.state='open' AND i.comment_id IS NOT NULL ORDER BY i.comment_id",
            (row["artifact_id"], row["reviewer_thread_id"]),
        ).fetchall()
        state = state or ("handed_back" if reason == "limit" else "failed")
        task_text = continuation([str(comment["id"]) for comment in comments], state, reason)
        request = {
            **dict(row),
            "requester_display": (requester["title"] if requester else None) or row["requester_id"],
            "note": f"{row['note'] or ''}\n\n{task_text}".strip(),
        }
        node = public.create_review_node(connection, row["reviewer_thread_id"], request)
        connection.execute(
            "UPDATE doc_review_requests SET node_id=?,fork_id=NULL WHERE id=?",
            (node.node_id, request_id),
        )
        transition(connection, request_id, state, reason=reason)
        artifact = models.get_artifact(row["artifact_id"], db=connection)
        if artifact is not None:
            notify_change(connection, request, artifact)
        after_commit(connection, lambda: public.notify_after_commit((public.get_node(node.node_id, db=connection).plan_id,), db=connection))
        return get_request(connection, request_id)


def cancel(db, request_id: str, actor: ReviewActor) -> dict:
    with write_transaction(db, scope="cancel_document_review") as connection:
        row = connection.execute("SELECT * FROM doc_review_requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            raise PermissionError(ARTIFACT_REFUSAL)
        artifact = _artifact(connection, row["artifact_id"], actor)
        if not may_cancel(connection, artifact, row, actor):
            raise PermissionError(CANCEL_REFUSAL)
        if not is_open(connection, row):
            return _view(connection, row)
        transition(connection, request_id, "withdrawn")
        if row["state"] in FALLBACK_STATES:
            public.close_review_node(connection, row["node_id"], reason=CANCEL_REASON)
        result = get_request(connection, request_id)
        notify_change(connection, result, artifact)
        return result


def _latest(db, artifact_id: str, reviewer_thread_id: str):
    """The reviewer's latest request on this document, whatever its state."""
    return db.execute(
        "SELECT * FROM doc_review_requests WHERE artifact_id=? AND reviewer_thread_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
        (artifact_id, reviewer_thread_id)).fetchone()
