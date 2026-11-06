"""Document-target resolution and shared validation for the document comment tools.

``tools.documents`` re-exports these under the private names they had there, since sibling
tool modules and the live-preview comment path already reach them through that module.
"""

from __future__ import annotations

import json
from typing import Any

from doceditor.comment_action_errors import CommentActionError
from doceditor.comment_actions import validate_create
from doceditor.comments import get_comment, validate_client_id
from infra.auth import may_access_artifact
from registry import models as registry_models
from registry import service as registry_service


def json_result(data: dict[str, Any]) -> str:
    return json.dumps(data, sort_keys=True)


def resolve_doc_target(
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


def validate_comment_request(body, parent_id, review, quote, replacement, doc_path, artifact_id, client_id,
                             region=None, insert_after=False):
    """Map shared creation validation to the native-tool error surface."""
    try:
        text = validate_create(body=body, parent_id=parent_id, review=review, quote=quote,
                               replacement=replacement, doc_path=doc_path, artifact_id=artifact_id, region=region,
                               insert_after=insert_after)
        return None, text, validate_client_id(client_id)
    except (CommentActionError, ValueError) as exc:
        return f"Error: {exc}", "", None


def resolve_comment_target(db, actor_thread_id, actor, parent_id, kwargs):
    """Authorize the target without reading loop-owned anchor state."""
    register_spec = doc_path = None
    if parent_id:
        parent = get_comment(parent_id, db=db)
        art = registry_models.get_artifact(parent["artifact_id"], db=db) if parent else None
        if parent is None or art is None or not may_access_artifact(
            art["parent_thread_id"], actor["user_id"], db=db
        ):
            raise PermissionError("parent_id does not reference an accessible comment")
        artifact_id = parent["artifact_id"]
    else:
        artifact_id, doc_path, register_spec = resolve_doc_target(
            doc_path=kwargs.get("doc_path"),
            artifact_id=kwargs.get("artifact_id"),
            actor_thread_id=actor_thread_id,
            user_id=actor["user_id"],
            db=db,
            allow_register=True,
        )
    return artifact_id, doc_path, register_spec
