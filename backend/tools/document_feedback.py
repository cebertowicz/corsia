"""Native tool for sending an agent's pending document feedback."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from doceditor import review_feedback
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db
from infra.queries import get_thread_for_user
from tools.artifact_tool_helpers import default_workspace, tool_path
from tools.base import Tool
from tools.delegation import _require_context, _tool_schema
from tools.documents import _json, _resolve_doc_target


def _delivery_text(pending: bool, undelivered: list[dict]) -> str:
    if undelivered:
        reason = undelivered[0]["undelivered_reason"].replace("_", " ")
        return (
            f"Feedback is still undelivered: {reason}. Retry with "
            "doc_feedback_send once the owner can receive it."
        )
    return "Feedback was delivered to the owner." if pending else "Nothing was pending for this document."


def _send_feedback(
    actor_thread_id: str,
    actor: dict[str, Any],
    artifact_id: str | None,
    doc_path: str | None,
    review_request_id: str | None = None,
) -> dict[str, Any]:
    """Close and retry one agent's batches in the canonical state owner."""
    db = get_db()
    try:
        authoring = get_thread_for_user(actor_thread_id, actor["user_id"], db=db)
        if authoring is None:
            raise PermissionError(
                "current thread is not owned by the authenticated principal"
            )
        resolved_id, _path, _spec = _resolve_doc_target(
            doc_path=doc_path,
            artifact_id=artifact_id,
            actor_thread_id=actor_thread_id,
            user_id=actor["user_id"],
            db=db,
            allow_register=False,
        )
        if resolved_id is None:
            raise ValueError(
                "doc_path is not registered; register it first or retry with its artifact_id"
            )
        with write_transaction(db, scope="send_agent_doc_feedback") as connection:
            if review_request_id is not None:
                pending, woken, undelivered = review_feedback.submit_fork_feedback(
                    connection, review_request_id, actor_thread_id, resolved_id,
                )
            else:
                pending, woken, undelivered = review_feedback.send_document_feedback(
                    connection, actor_thread_id, resolved_id,
                )
        result = _delivery_text(pending, undelivered)
        return {
            "artifact_id": resolved_id,
            "woken": woken,
            "undelivered_batches": undelivered,
            "result": result,
        }
    finally:
        db.close()


class DocFeedbackSendTool(Tool):
    """Send or retry the current agent's feedback for one document."""

    def __init__(self, workspace: Path | None = None):
        self._workspace = default_workspace(workspace)

    @property
    def name(self) -> str:
        return "doc_feedback_send"

    @property
    def description(self) -> str:
        return ("Send this document's pending feedback to the owner now, delivering your "
                "open batch and retrying any undelivered one.")

    @property
    def parameters(self) -> dict[str, Any]:
        return _tool_schema({
            "artifact_id": {"type": ["string", "null"]},
            "doc_path": {"type": ["string", "null"]},
        })

    async def execute(self, **kwargs: Any) -> str:
        context = _require_context(kwargs)
        if isinstance(context, str):
            return context
        actor_thread_id, actor = context
        artifact_id = kwargs.get("artifact_id")
        doc_path = kwargs.get("doc_path")
        if bool(artifact_id) == bool(doc_path):
            return "Error: pass exactly one of artifact_id or doc_path"
        if doc_path:
            doc_path = tool_path(doc_path, self._workspace)
        try:
            result = await run_in_db(
                _send_feedback, actor_thread_id, actor, artifact_id, doc_path, kwargs.get("_review_request_id"),
            )
        except PermissionError as exc:
            if str(exc) == "Artifact not found":
                return "Error: artifact is inaccessible; use an artifact owned by the current principal"
            return f"Error: {exc}"
        except ValueError as exc:
            if str(exc).startswith("artifact_id does not resolve"):
                return f"Error: {exc}; use an active accessible artifact_id or registered doc_path"
            if str(exc).startswith("doc_path is outside"):
                return f"Error: {exc}; use a path under a registrable root or its artifact_id"
            return f"Error: {exc}"
        except Exception as exc:
            return f"Error sending document feedback: {exc}"
        if result["woken"]:
            from plans import public

            await public.drive_threads(result["woken"])
        return _json(result)
