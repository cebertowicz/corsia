"""Doc-comments API for artifact-backed document comments.

- ``GET    /api/artifacts/{aid}/comments``            → threaded list
- ``POST   /api/artifacts/{aid}/comments``            → create (top / reply / review)
- ``POST   /api/artifacts/{aid}/comments/positions``  → batch-persist remapped offsets
- ``PATCH  /api/comments/{cid}``                      → resolve / ack / body / offsets

Storage lives in ``doceditor.comments``. Agent replies are routed by
event-routing, not created here.

Every endpoint is ownership-scoped: a comment belongs to its artifact, an
artifact to its owning thread, and a thread to a user. Ownership failures
resolve to 404 so foreign ids stay non-enumerable.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.doc_comment_models import CommentCreate

from doceditor import comments as store, feedback_batches, feedback_state, image_regions
from doceditor.comment_anchoring import anchored_threads
from doceditor.comment_creation import CommentSelectionChanged, create_validated_comment
from doceditor.comment_create_db import CommentAccessDenied, CommentInput
from infra.app_loop import AppLoopUnavailable
from doceditor.comment_feedback_transitions import apply_user_transition
from doceditor.comment_state_updates import update_comment
from doceditor.comment_actions import (
    CommentActionError, SUGGESTION_REASON_MAX_CHARS, check_user_text_edit, decide_all_from_author,
    decide_suggestion, reopen_fields, validate_create,
)
from doceditor.comments import CrossArtifactIdError
from doceditor.doc_text import read_doc_text
from doceditor.quote_anchor import anchor_context
from infra.auth import authenticate, may_access_artifact, require_user_header_only
from infra.database import get_db, write_transaction
from api.doc_comment_decisions import comment_action_status, decision_refusal
from api.files import document_save_http_exception
from doceditor.document_save import DocumentSave, DocumentSaveError, save_document_text
from infra.db_async import run_in_db
from registry import models as registry_models

router = APIRouter(prefix="/api", tags=["doc-comments"])


def _assert_owns_artifact(artifact_id: str, user_id: str, db) -> None:
    """Raise 404 unless ``user_id`` may access ``artifact_id``'s comments.

    The single ownership gate the comment routes share: artifact →
    ``parent_thread_id`` → ``threads.user_id``. Allowed when the caller owns
    the owning thread or the owner is a workspace/project sentinel (shared
    substrate). A real OTHER user's thread, or an absent artifact, is a 404 —
    foreign ids stay non-enumerable.
    """
    art = registry_models.get_artifact(artifact_id, db=db)
    if art is None or not may_access_artifact(art["parent_thread_id"], user_id, db=db):
        raise HTTPException(404, "Artifact not found")


@router.get("/artifacts/{artifact_id}/comments")
async def list_comments(artifact_id: str, token: str):
    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    def _list():
        db = get_db()
        try:
            _assert_owns_artifact(artifact_id, user["id"], db)
            artifact = registry_models.get_artifact(artifact_id, db=db)
            path = artifact["path"] if artifact else None
            comments, anchors_checked = anchored_threads(artifact_id, path, db)
            image_regions.annotate_stale(comments, path)
            feedback_state.annotate_reply_waiting_threads(db, user["id"], artifact_id, comments)
            feedback = feedback_batches.user_feedback_for_document(db, artifact_id)
            return comments, anchors_checked, feedback
        finally:
            db.close()

    comments, anchors_checked, feedback = await run_in_db(_list)
    return {"comments": comments, "anchors_checked": anchors_checked, "feedback": feedback}


@router.post("/artifacts/{artifact_id}/comments")
async def create_comment(artifact_id: str, token: str, body: CommentCreate):
    """Create a comment. Idempotent on ``client_id``."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    if body.replacement is None:
        text = body.body.strip()
        if not text:
            raise HTTPException(400, "Empty comment")
    else:
        try:
            text = validate_create(
                body=body.body, parent_id=body.parent_id, review=body.review,
                quote=body.anchor_text, replacement=body.replacement,
                doc_path=None, artifact_id=artifact_id,
            )
        except CommentActionError as exc:
            raise HTTPException(400, exc.message) from exc
    try:
        region = image_regions.check_region_shape(
            region=body.region, parent_id=body.parent_id, review=body.review,
            quote=body.anchor_text, replacement=body.replacement,
        )
    except CommentActionError as exc:
        raise HTTPException(400, exc.message) from exc

    def _access():
        db = get_db()
        try:
            _assert_owns_artifact(artifact_id, user["id"], db)
            artifact = registry_models.get_artifact(artifact_id, db=db)
            return artifact["path"] if artifact else None
        finally:
            db.close()

    path = await run_in_db(_access)
    if region is not None and not image_regions.is_image_path(path):
        raise HTTPException(400, "region applies only to an image artifact.")

    if body.anchor_text and not body.parent_id and not body.review and (body.from_off is None or body.to_off is None):
        raise HTTPException(400, "Anchored comments require selection offsets")
    request = CommentInput(
        artifact_id=artifact_id, path=path, body=text, author=user["name"], author_kind="user", user_id=user["id"],
        parent_id=body.parent_id, review=body.review, replacement=body.replacement,
        quote=body.anchor_text, start=body.from_off, end=body.to_off, browser=True, live_capture=body.live_capture,
        region=region,
    )
    try:
        row, _registered, feedback = await create_validated_comment(request, body.client_id)
    except CommentAccessDenied as exc:
        raise HTTPException(404, str(exc)) from exc
    except CommentActionError as exc:
        raise HTTPException(400, exc.message) from exc
    except (CommentSelectionChanged, AppLoopUnavailable) as exc:
        raise HTTPException(409, str(exc)) from exc
    except CrossArtifactIdError as exc:
        raise HTTPException(
            409,
            "client_id collides with another artifact's comment — re-mint and retry",
        ) from exc
    return {"comment": row, **feedback}


class FeedbackDocumentAction(BaseModel):
    artifact_id: str


def _user_feedback_write(user_id: str, artifact_id: str, action: str) -> dict:
    db = get_db()
    try:
        _assert_owns_artifact(artifact_id, user_id, db)
        with write_transaction(db, scope=f"user_doc_feedback_{action}") as connection:
            woken: list[str] = []
            if action == "send":
                woken = feedback_batches.close_for_document(
                    connection,
                    author_kind="user",
                    author_id=feedback_batches.USER_AUTHOR_ID,
                    artifact_id=artifact_id,
                    reason="send",
                )
            elif action == "leave":
                feedback_batches.mark_user_left(connection, artifact_id)
            elif action == "return":
                feedback_batches.mark_user_returned(connection, artifact_id)
            else:
                woken = feedback_batches.retry_for_document(
                    connection,
                    author_kind="user",
                    author_id=feedback_batches.USER_AUTHOR_ID,
                    artifact_id=artifact_id,
                )
            undelivered = feedback_batches.undelivered_for_document(
                connection,
                author_kind="user",
                author_id=feedback_batches.USER_AUTHOR_ID,
                artifact_id=artifact_id,
            )
        return {"woken": woken, "undelivered_batches": undelivered}
    finally:
        db.close()


async def _user_feedback_action(user_id: str, body: FeedbackDocumentAction, action: str) -> dict:
    result = await run_in_db(_user_feedback_write, user_id, body.artifact_id, action)
    if result["woken"]:
        from plans import public

        await public.drive_threads(result["woken"])
    return result


@router.post("/doc-feedback/send")
async def send_user_feedback(
    body: FeedbackDocumentAction, user: dict = Depends(require_user_header_only),
):
    """Close and deliver the user's open batches for one document."""
    return await _user_feedback_action(user["id"], body, "send")


@router.post("/doc-feedback/leave")
async def leave_user_feedback(
    body: FeedbackDocumentAction, user: dict = Depends(require_user_header_only),
):
    """Start the user's leave grace period for one document."""
    return await _user_feedback_action(user["id"], body, "leave")


@router.post("/doc-feedback/return")
async def return_user_feedback(
    body: FeedbackDocumentAction, user: dict = Depends(require_user_header_only),
):
    """Record that the user is looking at the document again: cancel leave grace.

    Reply markers are not touched here; each clears only when its thread's reply is shown (the /viewed stamp).
    """
    return await _user_feedback_action(user["id"], body, "return")


@router.post("/doc-feedback/retry")
async def retry_user_feedback(
    body: FeedbackDocumentAction, user: dict = Depends(require_user_header_only),
):
    """Retry the user's undelivered batches for one document."""
    return await _user_feedback_action(user["id"], body, "retry")


class PositionUpdates(BaseModel):
    updates: list[dict]  # [{id, from_off, to_off}]


@router.post("/artifacts/{artifact_id}/comments/positions")
async def update_positions(artifact_id: str, token: str, body: PositionUpdates):
    """Persist client-remapped anchor offsets (batched on autosave flush)."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    def _update():
        db = get_db()
        try:
            _assert_owns_artifact(artifact_id, user["id"], db)
            artifact = registry_models.get_artifact(artifact_id, db=db)
            from doceditor.live_tools import find_live
            if artifact and artifact.get("path") and find_live(Path(artifact["path"])) is not None:
                raise HTTPException(409, "Live comment positions are saved by the server")
            try:
                text = read_doc_text(artifact["path"] if artifact else None)
            except (OSError, ValueError):
                text = None
            updates = []
            for update in body.updates:
                prepared = dict(update)
                comment = store.get_comment(str(update.get("id", "")), db=db)
                if text is not None and comment and comment.get("anchor_text"):
                    context = anchor_context(
                        text,
                        update.get("from_off"),
                        update.get("to_off"),
                        comment["anchor_text"],
                    )
                    if context is not None:
                        prepared["anchor_prefix"], prepared["anchor_suffix"] = context
                updates.append(prepared)
            return store.update_positions(artifact_id, updates, db=db)
        finally:
            db.close()

    return {"updated": await run_in_db(_update)}


class CommentDecision(BaseModel):
    decision: str
    all_from_author: bool = False
    reason: str | None = Field(
        default=None,
        max_length=SUGGESTION_REASON_MAX_CHARS,
        json_schema_extra={"maxLength": SUGGESTION_REASON_MAX_CHARS},
    )



@router.post("/comments/{comment_id}/decision")
async def decide_comment(comment_id: str, token: str, body: CommentDecision):
    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    saved: dict = {}

    async def write(
        path: str, text: str, project_id: str | None, reason: str, expected_sha256: str | None,
    ) -> dict:
        try:
            receipt = await save_document_text(
                DocumentSave(path=path, text=text, project_id=project_id), user["id"], reason=reason,
                expected_sha256=expected_sha256,
            )
        except DocumentSaveError as exc:
            raise document_save_http_exception(exc) from exc
        saved.update(text=text, revision=receipt["revision"])
        return receipt

    try:
        decide = decide_all_from_author if body.all_from_author else decide_suggestion
        row = await decide(
            comment_id, body.decision, user_id=user["id"], decided_by=user["name"],
            actor_thread_id=None, write=write, reason=body.reason,
            **({} if body.all_from_author else {"include_document": True}))
    except CommentActionError as exc:
        return decision_refusal(exc)
    except DocumentSaveError as exc:
        raise document_save_http_exception(exc) from exc
    if body.all_from_author:
        return {**row, **saved}
    receipt = row.pop("_live_receipt", None)
    if receipt is not None:
        saved.update(text=receipt["text"], revision=receipt["revision"], server_saved=receipt["server_saved"])
    return {"comment": row, **saved}


class CommentPatch(BaseModel):
    resolved: bool | None = None
    acked: bool | None = None
    body: str | None = None
    from_off: int | None = None
    to_off: int | None = None
    anchor_text: str | None = None
    # Moves an image comment's pin; the pin then counts as placed on the current image.
    region: dict | None = None


@router.patch("/comments/{comment_id}")
async def patch_comment(comment_id: str, token: str, body: CommentPatch):
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "No fields to update")

    def _patch():
        db = get_db()
        try:
            artifact_id = store.get_artifact_id_for_comment(comment_id, db=db)
            if artifact_id is None:
                return None
            _assert_owns_artifact(artifact_id, user["id"], db)
            comment = store.get_comment(comment_id, db=db)
            try:
                check_user_text_edit(comment, fields)
                moved = fields
                if "region" in fields:
                    artifact = registry_models.get_artifact(artifact_id, db=db)
                    moved = image_regions.move_fields(comment, fields, artifact["path"] if artifact else None)
                patch = reopen_fields(comment, moved)
            except CommentActionError as exc:
                raise HTTPException(comment_action_status(exc), exc.message) from exc
            with write_transaction(db, scope="patch_user_comment") as connection:
                row = update_comment(connection, comment_id, patch, user_name=user["name"])
                apply_user_transition(connection, comment, patch, user)
                return row
        finally:
            db.close()

    row = await run_in_db(_patch)
    if row is None:
        raise HTTPException(404, "Comment not found")
    return {"comment": row}
