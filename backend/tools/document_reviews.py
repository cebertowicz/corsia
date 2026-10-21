"""Agent review-request tool and shared authenticated adapter execution."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from doceditor import review_requests as reviews
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db
from infra.write_effects import after_commit
from infra.queries import get_thread_for_user
from plans import public
from registry.registration_origin import RegistrationOrigin, origin_from_tool_context, resolve_rebind
from tools.artifact_tool_helpers import default_workspace, tool_path
from tools.base import Tool
from tools.delegation import _require_context, _tool_schema
from tools.documents import _json, _resolve_doc_target

TOOL_DESCRIPTION = (
    "Ask an agent whose scope this document touches to review it. Runs a short private "
    "review without interrupting the reviewer; its comments on this document reach the owner. "
    "If the review cannot finish, it becomes a reviewer plan task. The reviewer comments; it does not do the work. Ask only about a "
    "section that changes the reviewer's area: name that section and your question in `note`. "
    "The reviewer reads only that section and leaves its changes as suggestions. `reviewer` "
    "is a thread ID from `agent_roster`; the document's owner cannot be the reviewer. "
    "The reviewer is given the intent of the task you are on when you request (the task's own, "
    "or the one it inherits from the task it was handed for); request from that task."
)
TARGET_REFUSAL = "exactly one of doc_path / artifact_id is required"
UNREGISTERED_REFUSAL = "doc_path is not registered; register it first or retry with its artifact_id"
SESSION_REQUIRED = "as_session_id is required (the `corsia doc` CLI fills it automatically)"
SESSION_REFUSAL = "as_session_id is not owned by the authenticated principal"


def _start_after_commit(request_id: str) -> None:
    """Submit durable review work to the app loop after its request commits."""
    import infra.app_loop as app_loop
    from core.artifact_review_runtime import start

    app_loop.submit(start(request_id))


def _cancel_after_commit(fork_id: str) -> None:
    """Signal the running fork from its owner loop after cancellation commits."""
    from core.artifact_review_runtime import cancel

    cancel(fork_id)


def create_request(actor: reviews.ReviewActor, *, reviewer: str, note: str | None,
                   client_id: str, doc_path: str | None = None,
                   artifact_id: str | None = None,
                   origin: RegistrationOrigin | None = None) -> tuple[dict, list[str]]:
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
            # The requester's current task is the server Turn's live node, as artifact registration binds it.
            live = resolve_rebind(connection, actor.thread_id, origin)[0].plan_node_id
            woken: list[str] = []
            with public.collect_wake_recipients(connection, woken):
                result = reviews.request_review(
                    connection, aid, reviewer, actor, note, client_id, live_node_id=live,
                )
                if result["state"] == "forked":
                    after_commit(
                        connection,
                        lambda request_id=str(result["id"]): _start_after_commit(request_id),
                    )
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
                if result["state"] == "withdrawn" and result["fork_id"]:
                    after_commit(
                        connection,
                        lambda fork_id=str(result["fork_id"]): _cancel_after_commit(fork_id),
                    )
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
                "Stable retry key. Reuse it to retrieve the same request. To change a review "
                "already running, withdraw it and request again with your note.")},
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
                artifact_id=kwargs.get("artifact_id"),
                origin=origin_from_tool_context(thread_id, kwargs, "doc_review_request"))
        except reviews.ReviewRosterError:
            return f"Error: {reviews.ROSTER_ERROR}"
        except (ValueError, PermissionError) as exc:
            return f"Error: {exc}"
        await public.drive_threads(woken)
        return _json({"request": result})
