"""Agent-principal document comment routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from doceditor import comments as store, feedback_batches
from doceditor.comment_creation import create_validated_comment
from doceditor.comment_create_db import CommentAccessDenied, CommentInput
from infra.app_loop import AppLoopUnavailable
from api.doc_comments import comment_action_status, decision_refusal
from api.files import document_save_http_exception
from doceditor.document_save import DocumentSaveError, agent_document_writer
from doceditor.comment_actions import (
    SUGGESTION_REASON_MAX_CHARS, CommentActionError, check_agent_update, decide_all_from_author,
    decide_suggestion, reopen_fields, validate_create,
)
from doceditor.comment_action_errors import decision_only_error
from doceditor.comment_state_updates import update_comment
from doceditor.comments import CrossArtifactIdError, validate_client_id
from doceditor.quote_anchor import QuoteResolveError
from infra.auth import authenticate, may_access_artifact
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db
from infra.queries import get_thread_for_user
from registry import models as registry_models
from registry import service as registry_service
from registry.file_resolution import normalize_file_path, resolve_file_identity

router = APIRouter(prefix="/api/delegation/docs", tags=["delegation-docs"])


def _resolve_target(body: "AgentCommentCreate", user_id: str, db) -> tuple[str | None, str | None, dict | None]:
    """Resolve the comment target without writing registry rows."""
    if body.artifact_id:
        art = registry_models.get_artifact(body.artifact_id, db=db)
        if art is None or art["state"] != "active":
            raise HTTPException(
                422, f"artifact_id does not resolve to an active artifact: {body.artifact_id}")
        if not may_access_artifact(art["parent_thread_id"], user_id, db=db):
            raise HTTPException(404, "Artifact not found")
        return art["id"], art["path"], None

    normalized = normalize_file_path(body.doc_path, db=db)
    if normalized is None:
        raise HTTPException(400, f"doc_path is outside the registrable roots: {body.doc_path}")
    canonical, _zone, _tmp = normalized
    art = resolve_file_identity(canonical, db=db)
    if art is not None:
        if not may_access_artifact(art["parent_thread_id"], user_id, db=db):
            raise HTTPException(404, "Artifact not found")
        return art["id"], art["path"] or canonical, None
    register_spec = {"path": canonical}
    return None, canonical, register_spec


def _resolve_reply_target(body: "AgentCommentCreate", user_id: str, db) -> str:
    """Resolve a reply's inherited artifact."""
    parent = store.get_comment(body.parent_id, db=db)
    parent_art = (registry_models.get_artifact(parent["artifact_id"], db=db)
                  if parent else None)
    if parent is None or parent_art is None or not may_access_artifact(
            parent_art["parent_thread_id"], user_id, db=db):
        raise HTTPException(404, "parent_id does not reference an accessible comment")
    artifact_id = parent["artifact_id"]
    if body.doc_path or body.artifact_id:
        supplied_id, _dp, supplied_spec = _resolve_target(body, user_id, db)
        if supplied_spec is not None or supplied_id != artifact_id:
            raise HTTPException(
                400, "a reply's doc_path/artifact_id must match its parent's doc")
    return artifact_id


class AgentCommentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = ""
    doc_path: str | None = None
    artifact_id: str | None = None
    quote: str | None = None
    near: str | None = None
    parent_id: str | None = None
    review: bool = False
    replacement: str | None = None
    as_session_id: str | None = None
    client_id: str | None = None
    submit: bool = False

    @field_validator("client_id")
    @classmethod
    def _check_client_id(cls, v: str | None) -> str | None:
        return validate_client_id(v)


def _validate_create_shape(body: AgentCommentCreate) -> None:
    """Validate the request shape."""
    if body.replacement is not None:
        try:
            validate_create(body=body.body, parent_id=body.parent_id, review=body.review,
                            quote=body.quote, replacement=body.replacement,
                            doc_path=body.doc_path, artifact_id=body.artifact_id)
        except CommentActionError as exc:
            raise HTTPException(400, exc.message) from exc
    elif not body.body.strip():
        raise HTTPException(400, "Empty comment")
    if not body.as_session_id:
        raise HTTPException(
            400, "as_session_id is required (the `corsia doc` CLI fills it automatically)")
    is_reply, is_review = bool(body.parent_id), bool(body.review)
    if is_reply and is_review:
        raise HTTPException(400, "a reply cannot also be a whole-doc review")
    if (is_reply or is_review) and (body.quote or "").strip():
        raise HTTPException(400, "quote only applies to a top-level anchored comment")
    if is_reply:
        if body.doc_path and body.artifact_id:
            raise HTTPException(
                400, "a reply takes at most one of doc_path/artifact_id (it inherits the parent's doc)")
    else:
        if bool(body.doc_path) == bool(body.artifact_id):
            raise HTTPException(400, "exactly one of doc_path / artifact_id is required")
        if not is_review and not (body.quote or "").strip():
            raise HTTPException(400, "quote is required to anchor a top-level comment")


@router.post("/comments")
async def create_agent_comment(token: str, body: AgentCommentCreate):
    """Author a doc comment as an agent thread (top / reply / review)."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    _validate_create_shape(body)
    is_reply, is_review = bool(body.parent_id), bool(body.review)
    text = body.body.strip()

    def _preflight():
        db = get_db()
        try:
            authoring = get_thread_for_user(body.as_session_id, user["id"], db=db)
            if authoring is None:
                raise HTTPException(403, "as_session_id is not owned by the authenticated principal")
            author = (authoring["title"] or "").strip() or body.as_session_id
            if is_reply:
                return author, _resolve_reply_target(body, user["id"], db), None, None
            return (author, *_resolve_target(body, user["id"], db))
        finally:
            db.close()

    author, artifact_id, doc_path, register_spec = await run_in_db(_preflight)

    request = CommentInput(
        artifact_id=artifact_id, path=doc_path, body=text, author=author, author_kind="agent", user_id=user["id"],
        author_thread_id=body.as_session_id, parent_id=body.parent_id, review=is_review,
        quote=body.quote, near=body.near, replacement=body.replacement, registration=register_spec, submit=body.submit,
    )
    try:
        row, registered, feedback = await create_validated_comment(request, body.client_id)
    except CommentAccessDenied as exc:
        raise HTTPException(404, str(exc)) from exc
    except AppLoopUnavailable as exc:
        raise HTTPException(409, str(exc)) from exc
    except QuoteResolveError as exc:
        raise HTTPException(422, {"error": exc.kind, "message": exc.message, "candidate_lines": exc.candidate_lines}) from exc
    except OSError as exc:
        raise HTTPException(422, f"cannot read doc to anchor quote: {exc}") from exc
    except CrossArtifactIdError as exc:
        raise HTTPException(
            409, "client_id collides with another artifact's comment — re-mint and retry"
        ) from exc
    except ValueError as exc:
        detail = str(exc)
        status = 413 if "too large" in detail else 415 if "binary" in detail else 422
        raise HTTPException(status, "binary file — cannot anchor a quote" if status == 415 else detail) from exc
    if registered:
        registry_service.notify_changed(row["artifact_id"])
    if feedback["woken"]:
        from plans import public

        await public.drive_threads(feedback["woken"])
    return {"comment": row, **feedback}


class AgentFeedbackAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_path: str | None = None
    artifact_id: str | None = None
    as_session_id: str


@router.post("/feedback/{action}")
async def agent_feedback_action(action: str, token: str, body: AgentFeedbackAction):
    """Submit or retry one agent's feedback batches for a registered document."""
    if action not in {"submit", "retry"}:
        raise HTTPException(404, "Unknown feedback action")
    if bool(body.doc_path) == bool(body.artifact_id):
        raise HTTPException(400, "exactly one of doc_path / artifact_id is required")
    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    def _write():
        db = get_db()
        try:
            if get_thread_for_user(body.as_session_id, user["id"], db=db) is None:
                raise HTTPException(403, "as_session_id is not owned by the authenticated principal")
            artifact_id, _path, register_spec = _resolve_target(body, user["id"], db)
            if register_spec is not None or artifact_id is None:
                raise HTTPException(404, "No registered document feedback to deliver")
            with write_transaction(db, scope=f"agent_doc_feedback_{action}") as connection:
                if action == "submit":
                    woken = feedback_batches.close_for_document(
                        connection,
                        author_kind="agent",
                        author_id=body.as_session_id,
                        artifact_id=artifact_id,
                        reason="submit",
                    )
                else:
                    woken = feedback_batches.retry_for_document(
                        connection,
                        author_kind="agent",
                        author_id=body.as_session_id,
                        artifact_id=artifact_id,
                    )
                undelivered = feedback_batches.undelivered_for_document(
                    connection,
                    author_kind="agent",
                    author_id=body.as_session_id,
                    artifact_id=artifact_id,
                )
            return {"woken": woken, "undelivered_batches": undelivered}
        finally:
            db.close()

    result = await run_in_db(_write)
    if result["woken"]:
        from plans import public

        await public.drive_threads(result["woken"])
    return result


class AgentCommentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolved: bool | None = None
    acked: bool | None = None
    body: str | None = None
    from_off: int | None = None
    to_off: int | None = None
    anchor_text: str | None = None
    decision: str | None = None
    all_from_author: bool = False
    reason: str | None = Field(
        default=None, max_length=SUGGESTION_REASON_MAX_CHARS,
        json_schema_extra={"maxLength": SUGGESTION_REASON_MAX_CHARS})
    as_session_id: str | None = None


@router.patch("/comments/{comment_id}")
async def patch_agent_comment(comment_id: str, token: str, body: AgentCommentPatch):
    """Resolve / ack / edit a comment as an agent thread (mirrors PATCH
    /api/comments/{id})."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    if not body.as_session_id:
        raise HTTPException(
            400, "as_session_id is required (the `corsia doc` CLI fills it automatically)")
    if refusal := decision_only_error(
            decision=body.decision, all_from_author=body.all_from_author, reason=body.reason):
        raise HTTPException(400, refusal)
    if body.decision is not None:
        return await _decide_as_agent(comment_id, body, user)
    fields = {k: v for k, v in body.model_dump(exclude={"as_session_id", "decision", "all_from_author", "reason"}).items()
              if v is not None}
    if not fields:
        raise HTTPException(400, "No fields to update")
    if "body" in fields and not str(fields["body"]).strip():
        raise HTTPException(400, "Empty comment body")

    def _patch():
        db = get_db()
        try:
            if get_thread_for_user(body.as_session_id, user["id"], db=db) is None:
                raise HTTPException(403, "as_session_id is not owned by the authenticated principal")
            comment = store.get_comment(comment_id, db=db)
            if comment is None:
                return None
            art = registry_models.get_artifact(comment["artifact_id"], db=db)
            if art is None or not may_access_artifact(art["parent_thread_id"], user["id"], db=db):
                return None
            try:
                check_agent_update(
                    comment, fields, actor_thread_id=body.as_session_id,
                    owner_thread_id=art["parent_thread_id"], db=db)
                patch = reopen_fields(comment, fields)
            except CommentActionError as exc:
                raise HTTPException(comment_action_status(exc), exc.message) from exc
            with write_transaction(db, scope="patch_agent_comment") as connection:
                return update_comment(connection, comment_id, patch, actor_thread_id=body.as_session_id)
        finally:
            db.close()

    row = await run_in_db(_patch)
    if row is None:
        raise HTTPException(404, "Comment not found")
    return {"comment": row}


async def _decide_as_agent(comment_id: str, body: AgentCommentPatch, user: dict) -> dict | JSONResponse:
    """Accept or reject a suggestion as the ``as_session_id`` thread (``corsia doc accept|reject``)."""
    allowed = {"decision", "all_from_author", "reason", "as_session_id"}
    if any(key not in allowed for key in body.model_dump(exclude_none=True)):
        raise HTTPException(400, "decision cannot be combined with other fields")

    def _actor():
        db = get_db()
        try:
            return get_thread_for_user(body.as_session_id, user["id"], db=db)
        finally:
            db.close()

    actor = await run_in_db(_actor)
    if actor is None:
        raise HTTPException(403, "as_session_id is not owned by the authenticated principal")
    try:
        decide = decide_all_from_author if body.all_from_author else decide_suggestion
        row = await decide(
            comment_id, body.decision, user_id=user["id"],
            decided_by=(actor["title"] or "").strip() or body.as_session_id,
            actor_thread_id=body.as_session_id,
            write=agent_document_writer(body.as_session_id, user["id"]), reason=body.reason)
    except CommentActionError as exc:
        return decision_refusal(exc)
    except DocumentSaveError as exc:
        raise document_save_http_exception(exc) from exc
    if body.all_from_author:
        return row
    return {"comment": row}
