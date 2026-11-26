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
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from api.comment_post_timing import COMMENT_TIMING_HEADER, log_comment_timing
from api.doc_comment_models import CommentCreate

from doceditor import (
    comment_audience, comments as store, doc_images, feedback_batches, feedback_state,
    image_regions, snapshot_reanchor,
)
from doceditor.comment_anchoring import anchored_threads
from doceditor.comment_action_errors import CommentSelectionChanged
from doceditor.comment_creation import create_validated_comment
from doceditor.comment_create_db import CommentAccessDenied, CommentInput
from doceditor.comment_recipients import TagValidationError, trim_mentions
from doceditor.feedback_batch_core import PersonDocumentLane
from infra.app_loop import AppLoopUnavailable
from doceditor.comment_feedback_transitions import apply_user_transition
from doceditor.comment_state_updates import update_comment
from doceditor.comment_actions import (
    CommentActionError, SUGGESTION_REASON_MAX_CHARS, check_user_text_edit,
    decide_suggestion, reopen_fields, validate_create,
)
from doceditor.comment_batch_actions import accept_all_open, decide_all_from_author
from doceditor.comments import CommentTargetMoved, CrossArtifactIdError
from doceditor.doc_text import read_doc_text
from infra.auth import authenticate, require_user_header_only
from infra.database import get_db, write_transaction
from api.doc_comment_access import NotCommentable, _artifact_for_read, _artifact_for_write, _assert_owns_artifact, not_commentable_response
from api.doc_comment_decisions import comment_action_status, decision_refusal, selection_refusal
from api.files import document_save_http_exception
from doceditor.document_save import DocumentSave, DocumentSaveError, save_document_text
from infra.db_async import run_in_db
from registry import models as registry_models

router = APIRouter(prefix="/api", tags=["doc-comments"])


@router.get("/artifacts/{artifact_id}/comments")
async def list_comments(
    artifact_id: str, token: str,
    timing: Annotated[str | None, Header(alias=COMMENT_TIMING_HEADER)] = None,
):
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    log_comment_timing(timing, "GET", artifact_id)

    def _list():
        db = get_db()
        try:
            read_id = _artifact_for_read(artifact_id, user["id"], db)
            artifact = registry_models.get_artifact(read_id, db=db)
            path = artifact["path"] if artifact else None
            comments, anchors_checked = anchored_threads(read_id, path, db)
            image_regions.annotate_stale(db, comments, path, artifact["parent_thread_id"] if artifact else None)
            feedback_state.annotate_thread_attention(db, user["id"], read_id, comments)
            goes_to = comment_audience.annotate(db, artifact, comments)
            feedback = {**feedback_batches.user_feedback_for_document(db, read_id), "goes_to": goes_to}
            return comments, anchors_checked, feedback
        finally:
            db.close()

    comments, anchors_checked, feedback = await run_in_db(_list)
    return {"comments": comments, "anchors_checked": anchors_checked, "feedback": feedback}


@router.post("/artifacts/{artifact_id}/comments")
async def create_comment(
    artifact_id: str, token: str, body: CommentCreate,
    timing: Annotated[str | None, Header(alias=COMMENT_TIMING_HEADER)] = None,
):
    """Create a comment. Idempotent on ``client_id``."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    log_comment_timing(timing, "POST", artifact_id)
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
            region=body.region, parent_id=body.parent_id, review=body.review, replacement=body.replacement,
        )
    except CommentActionError as exc:
        raise HTTPException(400, exc.message) from exc

    def _access():
        db = get_db()
        try:
            live_id = _artifact_for_write(artifact_id, user["id"], db)
            artifact = registry_models.get_artifact(live_id, db=db)
            return live_id, artifact["path"] if artifact else None
        finally:
            db.close()

    try:
        artifact_id, path = await run_in_db(_access)
    except NotCommentable:
        return not_commentable_response()

    if body.anchor_text and not body.parent_id and not body.review and (body.from_off is None or body.to_off is None):
        raise HTTPException(400, "Anchored comments require selection offsets")
    try:
        mentions = trim_mentions(body.body, body.tagged_agent_ids, [mention.model_dump() for mention in body.tagged_agent_mentions])
    except TagValidationError as exc:
        return decision_refusal(exc)
    request = CommentInput(
        artifact_id=artifact_id, path=path, body=text, author=user["name"], author_kind="user", user_id=user["id"],
        parent_id=body.parent_id, review=body.review, replacement=body.replacement,
        quote=body.anchor_text, start=body.from_off, end=body.to_off, browser=True, live_capture=body.live_capture,
        region=region, diagram_source=body.diagram_source, kept_prefix=body.anchor_prefix, kept_suffix=body.anchor_suffix,
        delivery_policy=body.delivery_policy, tagged_agent_ids=body.tagged_agent_ids, viewing_thread_id=body.thread_id,
        tagged_agent_mentions=mentions,
    )
    try:
        row, _registered, feedback, _created = await create_validated_comment(request, body.client_id)
    except CommentTargetMoved:
        return not_commentable_response()
    except CommentAccessDenied as exc:
        raise HTTPException(404, str(exc)) from exc
    except TagValidationError as exc:
        # Its kind tells the browser which of the known causes to name.
        return decision_refusal(exc)
    except CommentActionError as exc:
        # Its kind rides along (as it does on a selection refusal) so the browser's timing report can name the refusal.
        return JSONResponse({"detail": exc.message, "kind": exc.kind}, status_code=400)
    except CommentSelectionChanged as exc:
        return selection_refusal(exc)
    except AppLoopUnavailable as exc:
        raise HTTPException(409, str(exc)) from exc
    except CrossArtifactIdError as exc:
        raise HTTPException(
            409,
            "client_id collides with another artifact's comment — re-mint and retry",
        ) from exc
    return {"comment": row, **feedback}


from api.doc_feedback_routes import add_feedback_routes

add_feedback_routes(router, _artifact_for_write)


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
            live_id = _artifact_for_write(artifact_id, user["id"], db)
            artifact = registry_models.get_artifact(live_id, db=db)
            from doceditor.live_tools import find_live
            if artifact and artifact.get("path") and find_live(Path(artifact["path"])) is not None:
                raise HTTPException(409, "Live comment positions are saved by the server")
            try:
                text = read_doc_text(artifact["path"] if artifact else None)
            except (OSError, ValueError):
                text = None
            def verify_text():
                current_artifact = registry_models.get_artifact(live_id, db=db)
                current_path = current_artifact.get('path') if current_artifact else None
                if current_path and find_live(Path(current_path)) is not None:
                    raise HTTPException(409, 'Live comment positions are saved by the server')
                try:
                    current_text = read_doc_text(current_path)
                except (OSError, ValueError):
                    current_text = None
                if current_path != (artifact.get('path') if artifact else None) or current_text != text:
                    raise HTTPException(409, 'Document changed while saving comment positions')
            try:
                return snapshot_reanchor.persist_client_positions(db, live_id, text, body.updates, verify_text=verify_text)
            except snapshot_reanchor.AnchorConflict as exc:
                raise HTTPException(409, 'Comment changed while saving positions') from exc
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



def _person_writer(user: dict, saved: dict):
    """The document writer a person's accept uses; it notes the text and revision it saved."""
    async def write(
        path: str, text: str, project_id: str | None, reason: str, expected_sha256: str | None,
        _suggestion_author: str | None, accepted: tuple[str, ...], *, suppress_feedback: bool = False,
    ) -> dict:
        receipt = await save_document_text(
            DocumentSave(path=path, text=text, project_id=project_id), user["id"], reason=reason,
            expected_sha256=expected_sha256, accepted=accepted, suppress_feedback=suppress_feedback,
        )
        saved.update(text=text, revision=receipt["revision"])
        return receipt
    return write


class SuggestionHistoryTransition(BaseModel):
    event_id: str
    ids: list[str]
    direction: str
    expected_sha256: str
    text: str


@router.post("/artifacts/{artifact_id}/suggestions/history")
async def suggestion_history_transition(artifact_id: str, token: str, body: SuggestionHistoryTransition):
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    from doceditor.suggestion_history_actions import transition_suggestion_history

    saved: dict = {}
    try:
        return await transition_suggestion_history(
            artifact_id, body.event_id, body.ids, body.direction, body.expected_sha256, body.text,
            user_id=user['id'], write=_person_writer(user, saved))
    except CommentActionError as exc:
        return decision_refusal(exc)
    except DocumentSaveError as exc:
        raise document_save_http_exception(exc) from exc


@router.post("/artifacts/{artifact_id}/suggestions/accept-all")
async def accept_all_suggestions(artifact_id: str, token: str):
    """Accept every open suggestion on the document, from every author, in one write."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    def _live():
        db = get_db()
        try:
            return _artifact_for_write(artifact_id, user["id"], db)
        finally:
            db.close()

    artifact_id = await run_in_db(_live)
    saved: dict = {}
    try:
        result = await accept_all_open(
            artifact_id, user_id=user["id"], decided_by=user["name"], actor_thread_id=None,
            write=_person_writer(user, saved))
    except CommentActionError as exc:
        return decision_refusal(exc)
    except DocumentSaveError as exc:
        raise document_save_http_exception(exc) from exc
    return {**result, **saved}


@router.post("/comments/{comment_id}/decision")
async def decide_comment(comment_id: str, token: str, body: CommentDecision):
    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    saved: dict = {}
    write = _person_writer(user, saved)

    try:
        decide = decide_all_from_author if body.all_from_author else decide_suggestion
        row = await decide(
            comment_id, body.decision, user_id=user["id"], decided_by=user["name"],
            actor_thread_id=None, write=write, reason=body.reason,
            **({} if body.all_from_author else {"include_document": True}))
    except CommentActionError as exc:
        return decision_refusal(exc)
    except DocumentSaveError as exc:
        if exc.kind == "no_such_file":
            return JSONResponse({"detail": exc.message, "kind": exc.kind}, status_code=404)
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
    # The lane a re-open queues in; the browser sends the person's document preference with every re-open.
    delivery_policy: PersonDocumentLane | None = None
    # Moves an image comment's pin; the pin then counts as placed on the current image.
    region: dict | None = None
    diagram_source: str | None = None

    @model_validator(mode="after")
    def _reopen_names_its_lane(self) -> "CommentPatch":
        """A re-open queues feedback, so it must say which lane; no other patch has one."""
        if self.resolved is False and self.delivery_policy is None:
            raise ValueError("delivery_policy is required when re-opening a comment")
        return self


@router.patch("/comments/{comment_id}")
async def patch_comment(comment_id: str, token: str, body: CommentPatch):
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    fields = {k: v for k, v in body.model_dump(exclude={"delivery_policy"}).items() if v is not None}
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
                artifact = registry_models.get_artifact(artifact_id, db=db) or {}
                moved = doc_images.region_patch(
                    db, comment, fields, doc_path=artifact.get("path"), owner_thread_id=artifact.get("parent_thread_id"),
                    require_diagram_source=True)
                patch = reopen_fields(comment, moved)
            except CommentActionError as exc:
                raise HTTPException(comment_action_status(exc), exc.message) from exc
            with write_transaction(db, scope="patch_user_comment") as connection:
                row = update_comment(connection, comment_id, patch, user_name=user["name"])
                apply_user_transition(connection, comment, patch, user, body.delivery_policy)
                return row
        finally:
            db.close()

    try:
        row = await run_in_db(_patch)
    except CommentSelectionChanged as exc:
        return selection_refusal(exc)
    if row is None:
        raise HTTPException(404, "Comment not found")
    return {"comment": row}
