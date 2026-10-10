"""Agent tool for requesting a document in the current thread's canvas."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from infra import presence, state
from infra.database import get_db
from infra.db_async import run_in_db
from infra.execution_class import is_subagent
from infra.product_events import emit_product_event
from infra.queries import get_thread_for_user
from tools.artifact_tool_helpers import default_workspace, tool_path
from tools.base import Tool
from tools.delegation import _require_context, _tool_schema
from tools.documents import _resolve_doc_target


_REQUESTED = (
    "Asked this thread's canvas to show it, on {count} screens where the user had "
    "this thread open. This is a request, not confirmation: a screen drops it if "
    "the user has just moved. Do not tell the user they have seen it. If you tell "
    "them it is in their canvas, do it in a `post_update` (load it by name if it "
    "is not listed)."
)
_VIEW_LINK = (
    "You cannot open it in the user's view. Give the user this link in a "
    "`post_update` (load it by name if it is not listed). Do not ask them to "
    "switch threads."
)
_CHILD_LINK = (
    "You cannot open documents for the user from a delegated thread. Return this "
    "link to your parent in your result."
)


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True)


def _link_result(reason: str, title: str, path: str) -> dict[str, Any]:
    guidance = _CHILD_LINK if reason == "delegated_thread" else _VIEW_LINK
    escaped_title = title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    escaped_path = path.replace("<", "%3C").replace(">", "%3E")
    return {
        "outcome": "link",
        "reason": reason,
        "link": f"[{escaped_title}](<{escaped_path}>)",
        "message": guidance,
    }


def _refused(reason: str) -> dict[str, Any]:
    return {"outcome": "refused", "reason": reason}


def _open_document(
    actor_thread_id: str,
    actor: dict[str, Any],
    artifact_id: str | None,
    path: str | None,
) -> dict[str, Any]:
    """Resolve one target, snapshot presence, and schedule its request once."""
    db = get_db()
    try:
        thread = get_thread_for_user(actor_thread_id, actor["user_id"], db=db)
        if thread is None:
            return _refused("no_access")
        owner_user_id = thread["user_id"]
        try:
            resolved_id, resolved_path, _spec = _resolve_doc_target(
                doc_path=path,
                artifact_id=artifact_id,
                actor_thread_id=actor_thread_id,
                user_id=actor["user_id"],
                db=db,
                allow_register=False,
            )
        except PermissionError:
            return _refused("no_access")
        except ValueError:
            return _refused("not_found")
        if resolved_id is None or resolved_path is None:
            return _refused("not_found")

        from registry import models as registry_models

        artifact = registry_models.get_artifact(resolved_id, db=db)
        if artifact is None:
            return _refused("not_found")
        if artifact["type"] != "document":
            return _refused("not_a_document")

        title = Path(resolved_path).name
        if is_subagent(thread):
            return _link_result("delegated_thread", title, resolved_path)

        snapshot = presence.document_open_snapshot(owner_user_id, actor_thread_id, resolved_id)
        if snapshot.verdict != "here":
            reason = "user_view_unknown" if snapshot.verdict == "unknown" else "user_not_viewing"
            return _link_result(reason, title, resolved_path)
        if snapshot.already_open:
            return {"outcome": "already_open", "reason": None, "count": 0}
        if not snapshot.eligible_session_ids:
            return _link_result("user_cannot_show", title, resolved_path)

        count = state.send_to_thread_sessions(
            actor_thread_id,
            snapshot.eligible_session_ids,
            {
                "type": "document_open_request",
                "thread_id": actor_thread_id,
                "artifact_id": resolved_id,
                "path": resolved_path,
                "title": title,
            },
        )
        if not count:
            return _link_result("user_view_unknown", title, resolved_path)
        return {"outcome": "requested", "reason": None, "count": count, "message": _REQUESTED.format(count=count)}
    finally:
        db.close()


class DocumentOpenTool(Tool):
    """Show a document to the user in this thread's canvas."""

    def __init__(self, workspace: Path | None = None):
        self._workspace = default_workspace(workspace)

    @property
    def name(self) -> str:
        return "document_open"

    @property
    def description(self) -> str:
        return "Show a document to the user in this thread's canvas"

    @property
    def parameters(self) -> dict[str, Any]:
        return _tool_schema({
            "path": {"type": ["string", "null"]},
            "artifact_id": {"type": ["string", "null"]},
        })

    async def execute(self, **kwargs: Any) -> str:
        context = _require_context(kwargs)
        if isinstance(context, str):
            return context
        actor_thread_id, actor = context
        artifact_id = kwargs.get("artifact_id")
        path = kwargs.get("path")
        if bool(artifact_id) == bool(path):
            return "Error: pass exactly one of path or artifact_id"
        if path:
            path = tool_path(path, self._workspace)
        result = await run_in_db(_open_document, actor_thread_id, actor, artifact_id, path)
        emit_product_event(
            "document",
            "agent_open",
            thread_id=actor_thread_id,
            user_id=actor["user_id"],
            properties={
                "outcome": result["outcome"],
                "reason": result["reason"],
                "count": result.get("count", 0),
            },
        )
        return _json(result)
