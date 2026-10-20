"""Agent review-request tool and shared authenticated adapter execution."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from doceditor import review_requests as reviews
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db
from infra.queries import get_thread_for_user
from plans import public
from tools.artifact_tool_helpers import default_workspace, tool_path
from tools.base import Tool
from tools.delegation import _require_context, _tool_schema
from tools.documents import _json, _resolve_doc_target

TOOL_DESCRIPTION = (
    "Ask an agent whose scope this document touches to review it. Queues a review "
    "task in the reviewer's plan; its comments on this document reach the owner "
    "and settle the task. The reviewer comments; it does not do the work. Ask only about a "
    "section that changes the reviewer's area: name that section and your question in `note`. "
    "The reviewer reads only that section and leaves its changes as suggestions. `reviewer` "
    "is a thread ID from `agent_roster`; the document's owner cannot be the reviewer."
)
TARGET_REFUSAL = "exactly one of doc_path / artifact_id is required"
UNREGISTERED_REFUSAL = "doc_path is not registered; register it first or retry with its artifact_id"
SESSION_REQUIRED = "as_session_id is required (the `corsia doc` CLI fills it automatically)"
SESSION_REFUSAL = "as_session_id is not owned by the authenticated principal"


def create_request(actor: reviews.ReviewActor, *, reviewer: str, note: str | None,
                   client_id: str, doc_path: str | None = None,
                   artifact_id: str | None = None) -> tuple[dict, list[str]]:
    if bool(doc_path) == bool(artifact_id):
        raise ValueError(TARGET_REFUSAL)
    db = get_db()
    try:
        with write_transaction(db, scope="create_review_adapter") as connection:
            if get_thread_for_user(actor.thread_id, actor.user_id, db=connection) is None:
                raise PermissionError(reviews.THREAD_REFUSAL)
            aid, _path, _spec = _resolve_doc_target(doc_path=doc_path, artifact_id=artifact_id,
                actor_thread_id=actor.thread_id, user_id=actor.user_id, db=connection, allow_register=False)
            if aid is None:
                raise ValueError(UNREGISTERED_REFUSAL)
            woken: list[str] = []
            with public.collect_wake_recipients(connection, woken):
                result = reviews.request_review(connection, aid, reviewer, actor, note, client_id)
        return result, woken
    finally:
        db.close()


def cancel_request(actor: reviews.ReviewActor, request_id: str) -> tuple[dict, list[str]]:
    db = get_db()
    try:
        with write_transaction(db, scope="cancel_review_adapter") as connection:
            woken: list[str] = []
            with public.collect_wake_recipients(connection, woken):
                result = reviews.cancel(connection, request_id, actor)
        return result, woken
    finally:
        db.close()


class DocReviewRequestTool(Tool):
    """Queue a named agent's review of one registered accessible document."""

    def __init__(self, workspace: Path | None = None):
        self._workspace = default_workspace(workspace)

    @property
    def name(self) -> str:
        return "doc_review_request"

    @property
    def description(self) -> str:
        return TOOL_DESCRIPTION

    @property
    def parameters(self) -> dict[str, Any]:
        return _tool_schema({
            "doc_path": {"type": ["string", "null"], "description": (
                "Registered document path. Supply exactly one of doc_path or artifact_id.")},
            "artifact_id": {"type": ["string", "null"], "description": (
                "Registered document ID. Supply exactly one of artifact_id or doc_path.")},
            "reviewer": {"type": "string", "description": (
                "Reviewer thread ID from agent_roster; must be an active agent in your visible roster.")},
            "note": {"type": ["string", "null"], "maxLength": 500},
            "client_id": {"type": "string", "maxLength": 64, "description": (
                "Stable retry key. Reuse it when retrying this request; a new key on an open "
                "request appends a supplied note once.")},
        }, ["reviewer", "client_id"])

    async def execute(self, **kwargs: Any) -> str:
        context = _require_context(kwargs)
        if isinstance(context, str):
            return context
        thread_id, thread = context
        actor = reviews.ReviewActor("agent", thread_id, thread_id, thread["user_id"], thread.get("title") or thread_id)
        doc_path = kwargs.get("doc_path")
        if doc_path:
            doc_path = tool_path(doc_path, self._workspace)
        try:
            result, woken = await run_in_db(create_request, actor,
                reviewer=kwargs.get("reviewer", ""), note=kwargs.get("note"),
                client_id=kwargs.get("client_id"), doc_path=doc_path,
                artifact_id=kwargs.get("artifact_id"))
        except reviews.ReviewRosterError:
            return f"Error: {reviews.ROSTER_ERROR}"
        except (ValueError, PermissionError) as exc:
            return f"Error: {exc}"
        await public.drive_threads(woken)
        return _json({"request": result})
