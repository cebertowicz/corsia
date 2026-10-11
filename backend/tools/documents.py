"""Document collaboration tools for agent harnesses."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.media_reads import IMAGE_KIND, MediaReadReference, MediaToolResult
from doceditor import comments as store, image_regions
from doceditor.comment_anchoring import anchored_threads
from doceditor.comment_creation import create_validated_comment
from doceditor.comment_create_db import CommentInput
from doceditor.document_save import DocumentSaveError, agent_document_writer

from doceditor.comment_actions import (
    CommentActionError, SUGGESTION_REASON_MAX_CHARS, check_agent_update, decide_all_from_author,
    decide_suggestion, reopen_fields, validate_create,
)
from doceditor.comment_action_errors import decision_only_error
from doceditor.comment_state_updates import update_comment
from doceditor.comments import CrossArtifactIdError, validate_client_id
from doceditor.quote_anchor import QuoteResolveError
from infra.auth import may_access_artifact
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db
from infra.queries import get_thread_for_user
from registry import models as registry_models
from registry import service as registry_service
from tools.artifact_tool_helpers import default_workspace, tool_path
from tools.base import Tool
from tools.delegation import _require_context, _tool_schema


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, sort_keys=True)


def _resolve_doc_target(
    *,
    doc_path: str | None,
    artifact_id: str | None,
    actor_thread_id: str,
    user_id: str,
    db,
    allow_register: bool,
) -> tuple[str | None, str | None, dict[str, Any] | None]:
    if artifact_id:
        art = registry_models.get_artifact(artifact_id, db=db)
        if art is None or art["state"] != "active":
            raise ValueError(f"artifact_id does not resolve to an active artifact: {artifact_id}")
        if not may_access_artifact(art["parent_thread_id"], user_id, db=db):
            raise PermissionError("Artifact not found")
        return art["id"], art["path"], None

    normalized = registry_service.normalize_file_path(doc_path, db=db)
    if normalized is None:
        raise ValueError(f"doc_path is outside the registrable roots: {doc_path}")
    canonical, _zone, _tmp = normalized
    art = registry_service.resolve_file_identity(canonical, db=db)
    if art is not None:
        if not may_access_artifact(art["parent_thread_id"], user_id, db=db):
            raise PermissionError("Artifact not found")
        return art["id"], art["path"] or canonical, None
    if not allow_register:
        return None, canonical, None
    return None, canonical, {"path": canonical}


def _validate_comment_request(body, parent_id, review, quote, replacement, doc_path, artifact_id, client_id,
                              region=None):
    """Map shared creation validation to the native-tool error surface."""
    try:
        text = validate_create(body=body, parent_id=parent_id, review=review, quote=quote,
                               replacement=replacement, doc_path=doc_path, artifact_id=artifact_id, region=region)
        return None, text, validate_client_id(client_id)
    except (CommentActionError, ValueError) as exc:
        return f"Error: {exc}", "", None


_REGION_SCHEMA = {
    "type": ["object", "null"],
    "properties": {"x": {"type": "number"}, "y": {"type": "number"},
                   "w": {"type": "number"}, "h": {"type": "number"}},
    "required": ["x", "y"],
    "description": ("On an image artifact: a box {x, y, w, h} (or a point {x, y}) in fractions 0..1 "
                    "of the image, origin top-left. Pass it instead of quote. The result includes the "
                    "image with your box drawn, so you can check it and move it with doc_comment_update."),
}


async def _region_result(
    payload: dict[str, Any], row: dict[str, Any], path: str | None, reader: str,
) -> str | MediaToolResult:
    """The JSON result plus the image with this comment's numbered box drawn (DCOL-FR13.7)."""
    def _render():
        db = get_db()
        try:
            return image_regions.write_marked_image(
                db, image=Path(str(path)), rows=[row], reader_thread_id=reader,
                label=f"{Path(str(path)).stem}-region-{row['region_number']}",
            )
        finally:
            db.close()

    try:
        marked = await run_in_db(_render)
    except image_regions.RENDER_ERRORS as exc:
        return _json({**payload, "marked_image_error": str(exc)})
    reference = MediaReadReference(
        path=str(marked.path), mime_type="image/png", kind=IMAGE_KIND,
        byte_size=marked.byte_size, sha256=marked.sha256,
    )
    text = _json({**payload, "marked_image": str(marked.path)})
    return MediaToolResult(f"{text}\n{reference.placeholder_text()}", reference)


def _resolve_comment_target(db, actor_thread_id, actor, parent_id, kwargs):
    """Authorize the target without reading loop-owned anchor state."""
    register_spec = doc_path = None
    if parent_id:
        parent = store.get_comment(parent_id, db=db)
        art = registry_models.get_artifact(parent["artifact_id"], db=db) if parent else None
        if parent is None or art is None or not may_access_artifact(
            art["parent_thread_id"], actor["user_id"], db=db
        ):
            raise PermissionError("parent_id does not reference an accessible comment")
        artifact_id = parent["artifact_id"]
    else:
        artifact_id, doc_path, register_spec = _resolve_doc_target(
            doc_path=kwargs.get("doc_path"),
            artifact_id=kwargs.get("artifact_id"),
            actor_thread_id=actor_thread_id,
            user_id=actor["user_id"],
            db=db,
            allow_register=True,
        )
    return artifact_id, doc_path, register_spec


class DocCommentsListTool(Tool):
    """List document comments for an artifact or registered path."""

    def __init__(self, workspace: Path | None = None):
        self._workspace = default_workspace(workspace)

    @property
    def name(self) -> str:
        return "doc_comments_list"

    @property
    def description(self) -> str:
        return "List comments for a document artifact or registered document path."

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

        def _list():
            db = get_db()
            try:
                aid, path, _spec = _resolve_doc_target(
                    doc_path=doc_path,
                    artifact_id=artifact_id,
                    actor_thread_id=actor_thread_id,
                    user_id=actor["user_id"],
                    db=db,
                    allow_register=False,
                )
                if aid is None:
                    return {"found": False, "comments": []}
                comments, anchors_checked = anchored_threads(aid, path, db)
                return {"found": True, "artifact_id": aid, "comments": comments,
                        "anchors_checked": anchors_checked}
            finally:
                db.close()

        try:
            return _json(await run_in_db(_list))
        except PermissionError:
            return "Error: Artifact not found"
        except Exception as e:
            return f"Error listing document comments: {e}"


class DocCommentCreateTool(Tool):
    """Create a document comment as the current agent thread."""

    def __init__(self, workspace: Path | None = None):
        self._workspace = default_workspace(workspace)

    @property
    def name(self) -> str:
        return "doc_comment_create"

    @property
    def description(self) -> str:
        return ("Create a document comment as the current agent thread. To suggest an edit, "
                "pass quote (the exact current text) and replacement (the new text); body is "
                "an optional note. To retry undelivered feedback, use doc_feedback_send.")

    @property
    def parameters(self) -> dict[str, Any]:
        return _tool_schema({
            "body": {"type": ["string", "null"]},
            "doc_path": {"type": ["string", "null"]},
            "artifact_id": {"type": ["string", "null"]},
            "quote": {"type": ["string", "null"]},
            "near": {"type": ["string", "null"]},
            "parent_id": {"type": ["string", "null"]},
            "review": {"type": "boolean"},
            "client_id": {"type": ["string", "null"]},
            "replacement": {"type": ["string", "null"]},
            "region": _REGION_SCHEMA,
            "submit": {
                "type": "boolean",
                "description": "Send this document's open feedback batch to its owner now.",
            },
        })

    async def execute(self, body: str | None = None, **kwargs: Any) -> str:
        context = _require_context(kwargs)
        if isinstance(context, str):
            return context
        actor_thread_id, actor = context
        parent_id = kwargs.get("parent_id")
        review = bool(kwargs.get("review"))
        error, text, client_id = _validate_comment_request(
            body,
            parent_id,
            review,
            kwargs.get("quote"),
            kwargs.get("replacement"),
            kwargs.get("doc_path"),
            kwargs.get("artifact_id"),
            kwargs.get("client_id"),
            kwargs.get("region"),
        )
        if error is not None:
            return error
        region = image_regions.parse_region(kwargs["region"]) if kwargs.get("region") is not None else None
        if kwargs.get("doc_path"):
            kwargs = {**kwargs, "doc_path": tool_path(kwargs["doc_path"], self._workspace)}

        def _preflight():
            db = get_db()
            try:
                authoring = get_thread_for_user(actor_thread_id, actor["user_id"], db=db)
                if authoring is None:
                    raise PermissionError("current session is not owned by the authenticated principal")
                author = (authoring["title"] or "").strip() or actor_thread_id
                return author, _resolve_comment_target(db, actor_thread_id, actor, parent_id, kwargs)
            finally:
                db.close()

        try:
            author, target = await run_in_db(_preflight)
            request = CommentInput(
                artifact_id=target[0], path=target[1], body=text, author=author, author_kind="agent", user_id=actor["user_id"],
                author_thread_id=actor_thread_id, parent_id=parent_id, review=review,
                replacement=kwargs.get("replacement"), quote=kwargs.get("quote"), near=kwargs.get("near"),
                registration=target[2], submit=bool(kwargs.get("submit")), region=region,
            )
            row, registered, feedback = await create_validated_comment(request, client_id)
        except QuoteResolveError as exc:
            return f"Error creating document comment: {dict(error=exc.kind, message=exc.message, candidate_lines=exc.candidate_lines)}"
        except CrossArtifactIdError:
            return "Error: client_id collides with another artifact's comment; re-mint and retry"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error creating document comment: {e}"
        if registered:
            registry_service.notify_changed(row["artifact_id"])
        if feedback["woken"]:
            from plans import public

            await public.drive_threads(feedback["woken"])
        if region is not None:
            return await _region_result({"comment": row, **feedback}, row, target[1], actor_thread_id)
        return _json({"comment": row, **feedback})


class DocCommentUpdateTool(Tool):
    """Resolve, acknowledge, or edit a document comment."""

    @property
    def name(self) -> str:
        return "doc_comment_update"

    @property
    def description(self) -> str:
        return (
            "Resolve, acknowledge, edit, or accept/reject a document suggestion as the current agent "
            "thread. You may edit only comments you wrote, and resolve or acknowledge only on a "
            "document you own or a comment you wrote; otherwise reply with doc_comment_create.")

    @property
    def parameters(self) -> dict[str, Any]:
        return _tool_schema({
            "comment_id": {"type": "string"},
            "resolved": {"type": ["boolean", "null"]},
            "acked": {"type": ["boolean", "null"]},
            "body": {"type": ["string", "null"]},
            "from_off": {"type": ["integer", "null"]},
            "to_off": {"type": ["integer", "null"]},
            "anchor_text": {"type": ["string", "null"]},
            "region": {**_REGION_SCHEMA, "description": (
                "Move your own image comment's box or point, in fractions 0..1 of the image. "
                "The result includes the image with the moved box drawn.")},
            "decision": {"type": ["string", "null"], "enum": ["accept", "reject"]},
            "all_from_author": {"type": "boolean"},
            "reason": {
                "type": ["string", "null"], "maxLength": SUGGESTION_REASON_MAX_CHARS,
                "description": "Optional verbatim reason for rejecting a suggestion (maximum 2,000 characters).",
            },
        }, ["comment_id"])

    async def execute(self, comment_id: str, **kwargs: Any) -> str:
        context = _require_context(kwargs)
        if isinstance(context, str):
            return context
        actor_thread_id, actor = context
        if refusal := decision_only_error(
                decision=kwargs.get("decision"), all_from_author=bool(kwargs.get("all_from_author")),
                reason=kwargs.get("reason")):
            return f"Error: {refusal}"
        if kwargs.get("decision") is not None:
            try:
                decide = decide_all_from_author if kwargs.get("all_from_author") else decide_suggestion
                row = await decide(
                    comment_id, kwargs["decision"], user_id=actor["user_id"],
                    decided_by=actor.get("title") or actor_thread_id,
                    actor_thread_id=actor_thread_id,
                    write=agent_document_writer(actor_thread_id, actor["user_id"]),
                    reason=kwargs.get("reason"))
            except CommentActionError as exc:
                return f"Error: {exc.message}"
            except DocumentSaveError as exc:
                if exc.kind in {"invalid_skill", "stale"}:
                    return f"Error: {exc.message}"
                detail = exc.detail if exc.detail is not None else exc.message
                return f"Error: the document could not be saved: {detail}"
            return _json(row if kwargs.get("all_from_author") else {"comment": row})
        fields = {
            k: kwargs.get(k)
            for k in ("resolved", "acked", "body", "from_off", "to_off", "anchor_text", "region")
            if kwargs.get(k) is not None
        }
        if not fields:
            return "Error: No fields to update"
        if "body" in fields and not str(fields["body"]).strip():
            return "Error: Empty comment body"

        def _update():
            db = get_db()
            try:
                comment = store.get_comment(comment_id, db=db)
                if comment is None:
                    return None
                art = registry_models.get_artifact(comment["artifact_id"], db=db)
                if art is None or not may_access_artifact(art["parent_thread_id"], actor["user_id"], db=db):
                    return None
                check_agent_update(
                    comment, fields, actor_thread_id=actor_thread_id,
                    owner_thread_id=art["parent_thread_id"], db=db)
                moved = image_regions.move_fields(comment, fields, art["path"]) if "region" in fields else fields
                patch = reopen_fields(comment, moved)
                with write_transaction(db, scope="patch_agent_comment") as connection:
                    return update_comment(connection, comment_id, patch, actor_thread_id=actor_thread_id), art["path"]
            finally:
                db.close()

        try:
            updated = await run_in_db(_update)
        except CommentActionError as exc:
            return f"Error: {exc.message}"
        row, path = updated or (None, None)
        if row is None:
            return "Error: Comment not found"
        if "region" in fields:
            return await _region_result({"comment": row}, row, path, actor_thread_id)
        return _json({"comment": row})
