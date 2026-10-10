"""Artifact registry tools for agent harnesses."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from infra.auth import may_access_artifact
from infra.database import get_db
from infra.db_async import run_in_db
from infra.queries import get_thread_for_user
from registry import models, service, view
from registry.registration_writer import origin_from_tool_context
from registry.removal_events import broadcast_artifact_removal_event
from tools.artifact_register_ops import register_for_tool
from tools.artifact_update import apply_artifact_update
from tools.base import Tool
from tools.delegation import _require_context, _tool_schema
from tools.artifact_tool_helpers import (
    causal_turn_id,
    default_workspace,
    to_json,
    tool_path,
)


def _resolve_by_args(kwargs: dict[str, Any], workspace: Path, db) -> dict | None:
    artifact_id = kwargs.get("artifact_id")
    if artifact_id:
        return models.get_artifact(artifact_id, db=db)
    path = kwargs.get("path")
    if path:
        return service.resolve_file_identity(tool_path(path, workspace), db=db)
    integration = kwargs.get("integration")
    type_ = kwargs.get("type")
    external_id = kwargs.get("external_id")
    if integration and type_ and external_id:
        return models.resolve(integration, type_, external_id, db=db)
    raise ValueError("pass artifact_id, path, or integration/type/external_id")


class ArtifactResolveTool(Tool):
    """Resolve an artifact by id, path, or identity triplet."""

    def __init__(self, workspace: Path | None = None):
        self._workspace = default_workspace(workspace)

    @property
    def name(self) -> str:
        return "artifact_resolve"

    @property
    def description(self) -> str:
        return "Resolve an artifact by artifact_id, workspace path, or integration/type/external_id."

    @property
    def parameters(self) -> dict[str, Any]:
        return _tool_schema({
            "artifact_id": {"type": ["string", "null"]},
            "path": {"type": ["string", "null"]},
            "integration": {"type": ["string", "null"]},
            "type": {"type": ["string", "null"]},
            "external_id": {"type": ["string", "null"]},
        })

    async def execute(self, **kwargs: Any) -> str:
        context = _require_context(kwargs)
        if isinstance(context, str):
            return context
        _sid, actor = context

        def _resolve():
            db = get_db()
            try:
                art = _resolve_by_args(kwargs, self._workspace, db)
                if art is None:
                    return {"found": False, "artifact": None}
                if not may_access_artifact(art["parent_thread_id"], actor["user_id"], db=db):
                    return {"found": False, "artifact": None}
                return {"found": True, "artifact": art}
            finally:
                db.close()

        try:
            return to_json(await run_in_db(_resolve))
        except Exception as e:
            return f"Error resolving artifact: {e}"


class ArtifactRegisterTool(Tool):
    """Register an artifact to a thread."""

    def __init__(self, workspace: Path | None = None):
        self._workspace = default_workspace(workspace)

    @property
    def name(self) -> str:
        return "artifact_register"

    @property
    def description(self) -> str:
        return "Register an artifact to the current or specified CORSIA thread."

    @property
    def parameters(self) -> dict[str, Any]:
        return _tool_schema({
            "integration": {"type": ["string", "null"]},
            "type": {"type": ["string", "null"]},
            "external_id": {"type": ["string", "null"]},
            "path": {"type": ["string", "null"]},
            "parent_thread_id": {"type": ["string", "null"]},
            "title": {"type": ["string", "null"]},
            "description": {"type": ["string", "null"]},
            "retention": {"type": ["string", "null"], "enum": ["ephemeral", "durable", None]},
            "metadata": {"type": ["object", "null"]},
        })

    async def execute(self, **kwargs: Any) -> str:
        context = _require_context(kwargs)
        if isinstance(context, str):
            return context
        actor_thread_id, actor = context
        parent_thread_id = kwargs.get("parent_thread_id") or actor_thread_id
        retention = kwargs.get("retention")
        if retention is not None and retention not in ("ephemeral", "durable"):
            return "Error: retention must be 'ephemeral' or 'durable'"

        origin = origin_from_tool_context(
            actor_thread_id, kwargs, "artifact_register"
        )
        status, result = await run_in_db(
            register_for_tool,
            parent_thread_id,
            actor["user_id"],
            kwargs,
            workspace=self._workspace,
            origin=origin,
        )
        if status == "not_found":
            return f"Error: Thread not found: {parent_thread_id}"
        if status == "bad_request":
            return f"Error: {result}"
        service.notify_changed(result["notification"])
        return to_json({"ok": True, "artifact": result["artifact"]})


class ArtifactUpdateTool(Tool):
    """Update artifact description or pane display metadata."""

    @property
    def name(self) -> str:
        return "artifact_update"

    @property
    def description(self) -> str:
        return (
            "Update an artifact description, pane visibility, persistence, or verification. "
            "Artifacts an agent writes while working on a plan task normally "
            "clear from the thread pane when the task finishes; durable "
            "documents under the project workspace, and pull requests, are "
            "exempt from that already. Set pinned for anything else a user "
            "will return to, so it stays and leads its list -- the top-level "
            "pane, or its own worktree group if the file lives in one. Set "
            "mark_verified after checking a shared document you own against "
            "its sources; that, not editing its Verified at header, clears "
            "never verified and review overdue."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return _tool_schema({
            "artifact_id": {"type": "string"},
            "description": {"type": ["string", "null"]},
            "hidden": {"type": ["boolean", "null"]},
            "pinned": {"type": ["boolean", "null"]},
            "mark_verified": {"type": ["boolean", "null"]},
            "thread_id": {"type": ["string", "null"]},
        }, ["artifact_id"])

    async def execute(self, artifact_id: str, **kwargs: Any) -> str:
        context = _require_context(kwargs)
        if isinstance(context, str):
            return context
        actor_thread_id, actor = context
        thread_id = kwargs.get("thread_id") or actor_thread_id
        causal_event_id = causal_turn_id(kwargs)

        try:
            result = await run_in_db(
                lambda: apply_artifact_update(
                    artifact_id,
                    kwargs,
                    actor_thread_id=actor_thread_id,
                    user_id=actor["user_id"],
                    thread_id=thread_id,
                    causal_event_id=causal_event_id,
                )
            )
        except (PermissionError, ValueError) as exc:
            return f"Error: {exc}"
        if result is None:
            return "Error: Artifact not found"
        row, surfacing_changed, event = result
        if surfacing_changed and event is not None:
            broadcast_artifact_removal_event(event)
        service.notify_changed(row)
        return to_json({"artifact": row})


class ArtifactReconcileTool(Tool):
    """Preview or apply known artifact pane repairs for a thread."""

    @property
    def name(self) -> str:
        return "artifact_reconcile"

    @property
    def description(self) -> str:
        return "Return a dry-run of known artifact pane repairs for a thread."

    @property
    def parameters(self) -> dict[str, Any]:
        return _tool_schema({
            "thread_id": {"type": ["string", "null"]},
            "mode": {"type": ["string", "null"], "enum": ["preview", "apply", None]},
        })

    async def execute(self, **kwargs: Any) -> str:
        context = _require_context(kwargs)
        if isinstance(context, str):
            return context
        actor_thread_id, actor = context
        thread_id = kwargs.get("thread_id") or actor_thread_id
        mode = kwargs.get("mode") or "preview"
        if mode not in ("preview", "apply"):
            return f"Error: unknown mode: {mode!r}"

        def _reconcile():
            db = get_db()
            try:
                if get_thread_for_user(thread_id, actor["user_id"], db=db) is None:
                    return None
                return view.reconcile_for_thread(thread_id, mode=mode)
            finally:
                db.close()

        result = await run_in_db(_reconcile)
        if result is None:
            return f"Error: Thread not found: {thread_id}"
        return to_json(result)


class ArtifactRelocateTool(Tool):
    """Move an artifact's backing file to a new local path, preserving identity."""

    def __init__(self, workspace: Path | None = None):
        self._workspace = default_workspace(workspace)

    @property
    def name(self) -> str:
        return "artifact_relocate"

    @property
    def description(self) -> str:
        return (
            "Move an artifact's backing file on the local filesystem, "
            "preserving its registry identity."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return _tool_schema({
            "artifact_id": {"type": "string"},
            "target_path": {"type": "string"},
        }, ["artifact_id", "target_path"])

    async def execute(self, artifact_id: str, target_path: str, **kwargs: Any) -> str:
        context = _require_context(kwargs)
        if isinstance(context, str):
            return context
        actor_thread_id, actor = context

        def _relocate():
            db = get_db()
            try:
                art = models.get_artifact(artifact_id, db=db)
                if art is None or not may_access_artifact(
                    art["parent_thread_id"], actor["user_id"], db=db
                ):
                    return "not_found", None
                from registry.relocate import RelocationError, relocate_artifact

                try:
                    artifact = relocate_artifact(
                        artifact_id, tool_path(target_path, self._workspace),
                        actor_thread_id=actor_thread_id, db=db,
                    )
                except RelocationError as exc:
                    return "bad_request", str(exc)
                return "ok", artifact
            finally:
                db.close()

        status, result = await run_in_db(_relocate)
        if status == "not_found":
            return "Error: Artifact not found"
        if status == "bad_request":
            return f"Error: {result}"
        return to_json({"artifact": result})
