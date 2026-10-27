"""Shared validation and decisions for anchored document comments and suggestions.

Every adapter (the user routes, the native agent tools, and the agent route the
``corsia doc`` CLI calls) goes through this module, so the create rules, the
stale check, the decider check and the decision write each live once. Accepting
writes the document through a writer the adapter injects, because the validated
document save lives in the API layer, which this module does not import.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from doceditor import comments as store, feedback_suggestion_decisions
from doceditor.comment_action_errors import CommentActionError
from doceditor.comment_validation import validate_create
from doceditor.doc_text import read_doc_text
from doceditor.quote_anchor import _cp_at_u16, reanchor
from doceditor.comment_permissions import check_agent_update, check_decider, owner_row
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db, run_off_db
from registry import models as registry_models

DECISIONS = {"accept": "accepted", "reject": "rejected"}
SUGGESTION_REASON_MAX_CHARS = 2_000

STALE_MESSAGE = (
    "The text this suggestion replaces is no longer in the document, so it cannot be applied. "
    "Reject it, or ask its author to suggest again against the current text."
)

INSERT_STALE_MESSAGE = (
    "The text this suggestion follows is no longer in the document, so it cannot be applied. "
    "Reject it, or ask its author to suggest again against the current text."
)

# Writes ``text`` to the document at ``path`` in scope ``project_id``, recording ``reason``
# where the save keeps history; raises when refused.
DocumentWriter = Callable[
    [str, str, str | None, str, str | None, str | None], Awaitable[dict[str, Any] | None]
]






def suggestion_span(comment: dict[str, Any], document_text: str) -> tuple[int, int]:
    """Resolve one suggestion's code-point span, or refuse it as stale."""
    if comment.get("replacement") is None:
        raise CommentActionError("not_suggestion", "This comment is not a suggestion.")
    span = reanchor(
        document_text, comment["anchor_text"], from16=comment.get("from_off"),
        to16=comment.get("to_off"), prefix=comment.get("anchor_prefix"),
        suffix=comment.get("anchor_suffix"),
    )
    stale_message = INSERT_STALE_MESSAGE if comment.get("insert_after") else STALE_MESSAGE
    if span is None:
        raise CommentActionError("stale", stale_message)
    start, end = _cp_at_u16(document_text, span[0]), _cp_at_u16(document_text, span[1])
    if start is None or end is None or document_text[start:end] != comment["anchor_text"]:
        raise CommentActionError("stale", stale_message)
    return start, end


def suggestion_edit_span(comment: dict[str, Any], span: tuple[int, int]) -> tuple[int, int]:
    """Map a resolved quoted span to the text the suggestion changes."""
    return (span[1], span[1]) if comment.get("insert_after") else span


def insertion_position(comment: dict[str, Any], document_text: str, point: int) -> int:
    """Skip accepted older insertions still contiguous after the same quote."""
    key = (comment.get("created_at", ""), comment["id"])
    anchor_end = point
    for sibling in sorted(comment.get("_accepted_insertions", []), key=lambda row: (row.get("created_at", ""), row["id"])):
        if (sibling.get("created_at", ""), sibling["id"]) >= key:
            break
        if sibling.get("anchor_text") != comment.get("anchor_text"):
            continue
        try:
            sibling_span = suggestion_span(sibling, document_text)
        except CommentActionError:
            continue
        if sibling_span[1] != anchor_end:
            continue
        replacement = sibling["replacement"]
        if not document_text.startswith(replacement, point):
            break
        point += len(replacement)
    return point


def splice_suggestion(document_text: str, comment: dict[str, Any], span: tuple[int, int]) -> str:
    """Apply a previously resolved suggestion span to ``document_text``."""
    start, end = span
    return document_text[:start] + comment["replacement"] + document_text[end:]


def applied_text(comment: dict[str, Any], document_text: str) -> str:
    """Return ``document_text`` with one re-anchored suggestion applied."""
    start, end = suggestion_edit_span(comment, suggestion_span(comment, document_text))
    if comment.get("insert_after"):
        start = end = insertion_position(comment, document_text, start)
    return splice_suggestion(document_text, comment, (start, end))


def _record_decision_row(
    connection, comment_id: str, *, decision: str, decided_by: str,
    decided_by_thread_id: str | None, decision_event_id: str, decided_at: str, bulk: bool = False,
) -> dict[str, Any]:
    row = store.update(comment_id, {
        "decision": DECISIONS[decision],
        "decided_by": decided_by,
        "decided_by_thread_id": decided_by_thread_id,
        "decided_at": decided_at,
        "decision_event_id": decision_event_id,
        "decided_in_bulk": bulk,
        "resolved": True,
        "acked": True,
    }, db=connection)
    if row is None:
        raise CommentActionError("not_found", "Comment not found")
    return row


def validate_decision_reason(reason: str | None) -> str | None:
    """Keep a verbatim optional decision reason within every adapter's bound."""
    if reason is not None and len(reason) > SUGGESTION_REASON_MAX_CHARS:
        raise CommentActionError(
            "reason_too_long",
            f"reason must be at most {SUGGESTION_REASON_MAX_CHARS} characters",
        )
    return reason


def record_decision(
    comment_id: str, *, decision: str, decided_by: str,
    decided_by_thread_id: str | None, reason: str | None = None, db=None,
) -> dict[str, Any]:
    """Record one decision and return the decided row."""
    reason = validate_decision_reason(reason)
    decided_at = datetime.now(timezone.utc).isoformat()
    decision_event_id = uuid.uuid4().hex
    with write_transaction(db, scope="record_doc_suggestion_decision") as connection:
        row = _record_decision_row(
            connection, comment_id, decision=decision, decided_by=decided_by,
            decided_by_thread_id=decided_by_thread_id, decision_event_id=decision_event_id,
            decided_at=decided_at,
        )
        feedback_suggestion_decisions.record_suggestion_decisions(
            connection, [row], decision=DECISIONS[decision], decided_by=decided_by,
            decided_by_thread_id=decided_by_thread_id, decision_event_id=decision_event_id,
            reason=reason,
        )
        return row


def check_user_text_edit(comment: dict[str, Any], fields: dict[str, Any]) -> None:
    """Refuse a user's edit to the text of a comment someone else wrote.

    The user may still reply to, resolve, re-open or re-anchor any comment
    (DCOL-FR10.3).
    """
    if "body" in fields and comment.get("author_kind") != "user":
        raise CommentActionError(
            "not_author",
            "Only the author can change a comment's text. Reply to it instead.",
        )


def reopen_fields(comment: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    """Apply the re-open rule to a comment patch.

    Re-opening a rejected suggestion clears its decision. An accepted one
    cannot re-open, because its edit is already in the text.
    """
    if fields.get("resolved") is True:
        if comment.get("replacement") is not None and not comment.get("decision"):
            raise CommentActionError(
                "suggestion_decision_required",
                "Accept or reject this suggestion instead of resolving it.",
            )
        fields = {**fields, "acked": True}
    if fields.get("resolved") is not False or not comment.get("decision"):
        return fields
    if comment["decision"] == DECISIONS["accept"]:
        raise CommentActionError(
            "reopen_accepted",
            "This suggestion was accepted, so its edit is already in the document and it cannot "
            "be re-opened. Suggest again to change the text.",
        )
    return {**fields, "decision": None, "decided_by": None,
            "decided_by_thread_id": None, "decided_at": None, "decision_event_id": None,
            "decided_in_bulk": None}


async def decide_suggestion(
    comment_id: str, decision: str, *, user_id: str, decided_by: str,
    actor_thread_id: str | None, write: DocumentWriter, include_document: bool = False,
    reason: str | None = None,
) -> dict[str, Any]:
    """Accept or reject one suggestion; the single path every adapter calls.

    Accept writes the applied text through ``write`` first and records the
    decision only after the write succeeds. If recording then fails, the error
    says the edit was applied, never success.
    """
    if decision not in DECISIONS:
        raise CommentActionError("decision", "decision must be accept or reject")
    reason = validate_decision_reason(reason)

    def _load():
        db = get_db()
        try:
            comment = store.get_comment(comment_id, db=db)
            artifact = registry_models.get_artifact(comment["artifact_id"], db=db) if comment else None
            if comment is None or artifact is None:
                raise CommentActionError("not_found", "Comment not found")
            owner = owner_row(db, artifact["parent_thread_id"])
            check_decider(user_id=user_id, actor_thread_id=actor_thread_id, artifact=artifact, db=db)
            if comment.get("insert_after"):
                comment["_accepted_insertions"] = [row for row in store.list_for_artifact(comment["artifact_id"], db=db)
                                                     if row.get("insert_after") and row.get("decision") == "accepted"]
            return comment, artifact, owner[1] if owner else None
        finally:
            db.close()

    comment, artifact, project_id = await run_in_db(_load)
    if comment.get("replacement") is None:
        raise CommentActionError("not_suggestion", "This comment is not a suggestion.")
    if comment.get("decision"):
        raise CommentActionError(
            "decided",
            f"This suggestion was already {comment['decision']}. Re-open a rejected suggestion "
            "before deciding it again.",
        )
    live_receipt = None
    if decision == "accept":
        from doceditor.live_suggestions import accept_live
        live_receipt = await accept_live(artifact, project_id, [comment], actor_thread_id, user_id=user_id, single=True)
        if live_receipt is None:
            document_text = await run_off_db(read_doc_text, artifact["path"])
            await write(
                artifact["path"], applied_text(comment, document_text), project_id,
                f"Accepted suggestion {comment_id} from {comment['author']}",
                hashlib.sha256(document_text.encode()).hexdigest(),
                str(comment["author"]) if comment["author_kind"] == "user" else None,
            )

    def _record():
        db = get_db()
        try:
            return record_decision(
                comment_id, decision=decision, decided_by=decided_by,
                decided_by_thread_id=actor_thread_id, reason=reason, db=db,
            )
        finally:
            db.close()

    try:
        row = await run_in_db(_record)
    except sqlite3.Error as exc:
        if decision != "accept":
            raise
        raise CommentActionError(
            "unrecorded",
            f"The edit was applied to the document, but the decision could not be recorded ({exc}). "
            "The suggestion now reads as stale; reject it to close it.",
        ) from exc
    if live_receipt is not None and include_document:
        row["_live_receipt"] = live_receipt
    return row
