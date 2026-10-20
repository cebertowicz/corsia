"""User and CLI review-request routes over one canonical service."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from doceditor import review_requests as reviews
from doceditor.review_request_view import document_reviews
from infra.auth import require_user, require_user_header_only
from infra.database import get_db
from infra.db_async import run_in_db
from infra.queries import get_thread_for_user
from plans import public
from tools.document_reviews import (
    SESSION_REFUSAL, SESSION_REQUIRED, cancel_request, create_request, refresh_request,
)

router = APIRouter(prefix="/api", tags=["doc-reviews"])


class ReviewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_id: str | None = None
    doc_path: str | None = None
    reviewer: str
    note: str | None = None
    client_id: str


class UserReviewCreate(ReviewCreate):
    thread_id: str


class AgentReviewCreate(ReviewCreate):
    as_session_id: str | None = None


def _actor(user: dict, thread_id: str, kind: str) -> reviews.ReviewActor:
    db = get_db()
    try:
        thread = get_thread_for_user(thread_id, user["id"], db=db)
        if thread is None:
            raise HTTPException(403 if kind == "agent" else 404,
                                SESSION_REFUSAL if kind == "agent" else reviews.THREAD_REFUSAL)
        return reviews.ReviewActor(kind, thread_id if kind == "agent" else user["id"],
            thread_id, user["id"], thread["title"] if kind == "agent" else user["name"])
    finally:
        db.close()


async def _create(body: ReviewCreate, actor: reviews.ReviewActor):
    try:
        result, woken = await run_in_db(create_request, actor, reviewer=body.reviewer,
            note=body.note, client_id=body.client_id, doc_path=body.doc_path,
            artifact_id=body.artifact_id)
    except PermissionError as exc:
        raise HTTPException(404, str(exc)) from exc
    except reviews.ReviewRosterError as exc:
        raise HTTPException(503, reviews.ROSTER_ERROR) from exc
    except reviews.ReviewRequestError as exc:
        detail = reviews.USER_ROSTER_REFUSAL if (
            actor.kind == "user" and exc.code == reviews.REVIEWER_NOT_IN_ROSTER
        ) else reviews.REVIEWER_REASON_COPY.get(exc.code, str(exc))
        raise HTTPException(400, detail) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await public.drive_threads(woken)
    if result["state"] == "forked":
        from core.artifact_review_runtime import start

        await start(result["id"])
        result = await run_in_db(refresh_request, result["id"])
    return {"request": result}


@router.post("/doc-reviews")
async def create_user_review(body: UserReviewCreate, user: dict = Depends(require_user_header_only)):
    actor = await run_in_db(_actor, user, body.thread_id, "user")
    return await _create(body, actor)


@router.post("/delegation/docs/reviews")
async def create_agent_review(body: AgentReviewCreate, user: dict = Depends(require_user)):
    if not body.as_session_id:
        raise HTTPException(400, SESSION_REQUIRED)
    actor = await run_in_db(_actor, user, body.as_session_id, "agent")
    return await _create(body, actor)


async def _cancel(request_id: str, actor: reviews.ReviewActor):
    try:
        result, woken = await run_in_db(cancel_request, actor, request_id)
    except PermissionError as exc:
        raise HTTPException(403 if str(exc) == reviews.CANCEL_REFUSAL else 404, str(exc)) from exc
    await public.drive_threads(woken)
    return {"request": result}


@router.delete("/doc-reviews/{request_id}")
async def cancel_user_review(request_id: str, user: dict = Depends(require_user_header_only)):
    return await _cancel(request_id, reviews.ReviewActor("user", user["id"], "", user["id"], user["name"]))


@router.delete("/delegation/docs/reviews/{request_id}")
async def cancel_agent_review(request_id: str, as_session_id: str | None = None,
                              user: dict = Depends(require_user)):
    if not as_session_id:
        raise HTTPException(400, SESSION_REQUIRED)
    actor = await run_in_db(_actor, user, as_session_id, "agent")
    return await _cancel(request_id, actor)


@router.get("/artifacts/{artifact_id}/reviews")
async def list_reviews(artifact_id: str, thread_id: str | None = None,
                       user: dict = Depends(require_user)):
    def read():
        db = get_db()
        try:
            actor = reviews.ReviewActor("user", user["id"], thread_id or "", user["id"], user["name"])
            return document_reviews(db, artifact_id, actor)
        finally:
            db.close()
    try:
        result = await run_in_db(read)
    except PermissionError as exc:
        raise HTTPException(404, str(exc)) from exc
    return result
