"""Batch decisions on document suggestions: every open one by an author, or every open one.

One suggestion's rules live in ``comment_actions``. A batch resolves, de-overlaps and
applies its suggestions against one document snapshot (or the live document), then
records every decision in one write. Every batch path shares ``_accept_batch`` and
``_record_batch``, so there is one writer and one decision rule.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from doceditor import comments as store, feedback_suggestion_decisions
from doceditor.comment_action_errors import CommentActionError
from doceditor.comment_actions import (
    DECISIONS, DocumentWriter, _record_decision_row, insertion_position, splice_suggestion, suggestion_span, suggestion_edit_span,
    validate_decision_reason,
)
from doceditor.comment_permissions import check_decider, owner_row
from doceditor.doc_text import read_doc_text
from doceditor.feedback_edit_discard import discard_new_edit_item
from doceditor.suggestion_overlap import is_open_suggestion, suggestions_overlap
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db, run_off_db
from registry import models as registry_models


def _open_author_suggestions(comment_id: str, user_id: str, actor_thread_id: str | None, decision: str):
    """Load the authorized author's open suggestions on this document once."""
    db = get_db()
    try:
        comment = store.get_comment(comment_id, db=db)
        artifact = registry_models.get_artifact(comment["artifact_id"], db=db) if comment else None
        if comment is None or artifact is None:
            raise CommentActionError("not_found", "Comment not found")
        owner = owner_row(db, artifact["parent_thread_id"])
        check_decider(user_id=user_id, actor_thread_id=actor_thread_id, artifact=artifact, db=db)
        rows = store.list_for_artifact(comment["artifact_id"], db=db)
        open_rows = [row for row in rows if is_open_suggestion(row)
                     and row.get("author_thread_id") == comment.get("author_thread_id")]
        if decision == "accept":
            _attach_accepted_insertions(open_rows, rows)
        if not open_rows:
            raise CommentActionError(
                "no_open_suggestions", f"No open suggestions from {comment['author']} on this document."
            )
        return artifact, owner[1] if owner else None, open_rows
    finally:
        db.close()


def _open_suggestions(artifact_id: str, user_id: str, actor_thread_id: str | None):
    """Load every open suggestion on the document, from any author, once; there may be none."""
    db = get_db()
    try:
        artifact = registry_models.get_artifact(artifact_id, db=db)
        if artifact is None:
            raise CommentActionError("not_found", "Document not found")
        owner = owner_row(db, artifact["parent_thread_id"])
        check_decider(user_id=user_id, actor_thread_id=actor_thread_id, artifact=artifact, db=db)
        all_rows = store.list_for_artifact(artifact_id, db=db)
        rows = [row for row in all_rows if is_open_suggestion(row)]
        _attach_accepted_insertions(rows, all_rows)
        return artifact, owner[1] if owner else None, rows
    finally:
        db.close()


def _attach_accepted_insertions(rows: list[dict], all_rows: list[dict]) -> None:
    siblings: dict[tuple, list[dict]] = {}
    for row in all_rows:
        if store.is_accepted_insertion(row):
            siblings.setdefault((row["artifact_id"], row["anchor_text"]), []).append(row)
    for row in rows:
        if row.get("insert_after"):
            row["_accepted_insertions"] = siblings.get((row["artifact_id"], row["anchor_text"]), [])


def _batch_text(rows: list[dict[str, Any]], document_text: str) -> tuple[str, list[str], list[dict[str, str]]]:
    """Resolve, de-overlap, and splice a batch against one document snapshot."""
    candidates: list[tuple[int, int, dict[str, Any], tuple[int, int]]] = []
    skipped: list[dict[str, str]] = []
    for row in rows:
        try:
            anchor_span = suggestion_span(row, document_text)
            start, end = suggestion_edit_span(row, anchor_span)
            if row.get("insert_after"):
                start = end = insertion_position(row, document_text, start)
        except CommentActionError as exc:
            skipped.append({"id": row["id"], "reason": exc.message})
        else:
            candidates.append((start, end, row, anchor_span))
    one_author = len({row.get("author_thread_id") for row in rows}) <= 1
    overlap = "overlaps an earlier suggestion" + (" from the same author" if one_author else "")
    winners: list[tuple[int, int, dict[str, Any], tuple[int, int]]] = []
    for candidate in sorted(candidates, key=lambda item: (item[0], item[1], item[2].get("created_at", ""), item[2]["id"])):
        # Only a batch sees the final point: accepted siblings can move it at decision time.
        if any(suggestions_overlap(candidate[2], candidate[3], row, span)
               or (candidate[2].get("insert_after") and start < candidate[0] < end)
               or (row.get("insert_after") and candidate[0] < start < candidate[1])
               for start, end, row, span in winners):
            skipped.append({"id": candidate[2]["id"], "reason": overlap})
        else:
            winners.append(candidate)
    text = document_text
    for start, end, row, _span in sorted(winners, key=lambda item: (item[0], item[1], item[2].get("created_at", ""), item[2]["id"]), reverse=True):
        text = splice_suggestion(text, row, (start, end))
    return text, [row["id"] for _start, _end, row, _span in winners], skipped


def _drop_edit_feedback(artifact_id: str, text: str) -> None:
    db = get_db()
    try:
        with write_transaction(db, scope="discard_accepted_suggestion_edit") as connection:
            discard_new_edit_item(connection, artifact_id, text)
    finally:
        db.close()


def _accepted_user_author(rows: list[dict[str, Any]], accepted_ids: list[str]) -> str | None:
    """Return a user author only when every accepted row has the same stored author."""
    accepted = set(accepted_ids)
    winners = [row for row in rows if row.get("id") in accepted]
    if not accepted or len(winners) != len(accepted):
        return None
    if any(row.get("author_kind") != "user" for row in winners):
        return None
    authors = {row.get("author") for row in winners}
    if len(authors) != 1:
        return None
    author = authors.pop()
    return author if isinstance(author, str) and author else None


async def _accept_batch(
    artifact: dict[str, Any], project_id: str | None, rows: list[dict[str, Any]],
    actor_thread_id: str | None, *, user_id: str, write: DocumentWriter, suggestion_accept: bool = False,
) -> tuple[str, list[str], list[dict[str, str]], dict[str, Any] | None]:
    """Apply a batch through the live lane, or write it to disk when none is open.

    With ``suggestion_accept`` the write is not the person's own edit, so no edit feedback of
    theirs is left behind: the live lane drops it as it publishes, a plain write drops it here.
    """
    from doceditor.live_suggestions import accept_live
    receipt = await accept_live(
        artifact, project_id, rows, actor_thread_id, user_id=user_id, suggestion_accept=suggestion_accept)
    if receipt is not None:
        return receipt["text"], receipt["accepted"], receipt["skipped"], receipt
    source = await run_off_db(read_doc_text, artifact["path"])
    text, accepted, skipped = _batch_text(rows, source)
    if accepted:
        authors = ", ".join(dict.fromkeys(row["author"] for row in rows))
        suggestion_author = _accepted_user_author(rows, accepted)
        receipt = await write(
            artifact["path"], text, project_id, f"Accepted {len(accepted)} suggestions from {authors}",
            hashlib.sha256(source.encode()).hexdigest(), suggestion_author,
        )
        if suggestion_accept:
            await run_in_db(_drop_edit_feedback, artifact["id"], text)
    return text, accepted, skipped, receipt


def _record_batch(
    comment_ids: list[str], decision: str, *, decided_by: str, actor_thread_id: str | None, reason: str | None,
) -> None:
    """Record one decision on every id in one transaction, telling each agent author once."""
    db = get_db()
    try:
        with write_transaction(db, scope="record_suggestion_decisions") as connection:
            decided_at = datetime.now(timezone.utc).isoformat()
            decision_event_id = uuid.uuid4().hex
            decided_rows = [
                _record_decision_row(
                    connection, comment_id, decision=decision, decided_by=decided_by,
                    decided_by_thread_id=actor_thread_id, decision_event_id=decision_event_id,
                    decided_at=decided_at, bulk=True,
                )
                for comment_id in comment_ids
            ]
            by_author: dict[str | None, list[dict[str, Any]]] = {}
            for row in decided_rows:
                by_author.setdefault(row.get("author_thread_id"), []).append(row)
            for author_rows in by_author.values():
                feedback_suggestion_decisions.record_suggestion_decisions(
                    connection, author_rows, decision=DECISIONS[decision], decided_by=decided_by,
                    decided_by_thread_id=actor_thread_id, decision_event_id=decision_event_id,
                    reason=reason,
                )
    finally:
        db.close()


async def _decide_batch(
    artifact: dict[str, Any], project_id: str | None, rows: list[dict[str, Any]], decision: str, *,
    user_id: str, decided_by: str, actor_thread_id: str | None, write: DocumentWriter, reason: str | None,
    suggestion_accept: bool = False,
) -> dict[str, Any]:
    """Accept or reject loaded suggestions: apply the accepted ones first, then record the decisions."""
    if decision == "reject":
        await run_in_db(
            _record_batch, [row["id"] for row in rows], decision,
            decided_by=decided_by, actor_thread_id=actor_thread_id, reason=reason)
        return {"accepted": [], "skipped": [], "text": None, "revision": None}
    if not rows:
        return {"accepted": [], "skipped": [], "text": None, "revision": None}
    text, accepted, skipped, receipt = await _accept_batch(
        artifact, project_id, rows, actor_thread_id, user_id=user_id, write=write,
        suggestion_accept=suggestion_accept)
    if accepted:
        try:
            await run_in_db(
                _record_batch, accepted, decision,
                decided_by=decided_by, actor_thread_id=actor_thread_id, reason=reason)
        except sqlite3.Error as exc:
            raise CommentActionError(
                "unrecorded", f"The edit was applied to the document, but the decision could not be recorded ({exc}). "
                "The suggestions now read as stale; reject them to close them.",
            ) from exc
    return {
        "accepted": accepted,
        "skipped": skipped,
        "text": text,
        "revision": receipt.get("revision") if receipt else None,
    }


async def decide_all_from_author(
    comment_id: str, decision: str, *, user_id: str, decided_by: str,
    actor_thread_id: str | None, write: DocumentWriter, reason: str | None = None,
) -> dict[str, Any]:
    """Accept or reject every open suggestion by the selected comment's author."""
    if decision not in DECISIONS:
        raise CommentActionError("decision", "decision must be accept or reject")
    reason = validate_decision_reason(reason)
    artifact, project_id, rows = await run_in_db(_open_author_suggestions, comment_id, user_id, actor_thread_id, decision)
    return await _decide_batch(
        artifact, project_id, rows, decision, user_id=user_id, decided_by=decided_by,
        actor_thread_id=actor_thread_id, write=write, reason=reason)


async def accept_all_open(
    artifact_id: str, *, user_id: str, decided_by: str, actor_thread_id: str | None, write: DocumentWriter,
) -> dict[str, Any]:
    """Accept every open suggestion on the document, from every author, in one write.

    A suggestion whose text changed, or one that overlaps an earlier suggestion, is skipped
    and stays open; having none to accept is not an error.
    """
    artifact, project_id, rows = await run_in_db(_open_suggestions, artifact_id, user_id, actor_thread_id)
    return await _decide_batch(
        artifact, project_id, rows, "accept", user_id=user_id, decided_by=decided_by,
        actor_thread_id=actor_thread_id, write=write, reason=None, suggestion_accept=True)
