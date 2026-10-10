"""Ownership checks shared by document comment mutation adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from doceditor.comment_action_errors import CommentActionError
from infra.auth import may_access_artifact
from registry.shared_document_messages import accept_refusal
from registry.shared_principals import artifact_owner, document_scope, may_edit_document, owner_label, scope_relative


def owner_row(db, owner_thread_id: str | None):
    return db.execute("SELECT title, project_id FROM threads WHERE id=?", (owner_thread_id,)).fetchone()


def _require_owner(actor_thread_id: str, owner_thread_id: str | None, owner_title: str | None, *, also: str, instead: str) -> None:
    if actor_thread_id != owner_thread_id:
        raise CommentActionError("not_owner", f"Only the document's owner ({owner_title or owner_thread_id}) or {also}. {instead}")


def check_decider(*, user_id: str, actor_thread_id: str | None, artifact: dict, db=None) -> None:
    """Accepting or rejecting a suggestion edits the document, so it asks the one edit checker.

    The tenant check stays on the thread that holds the artifact; who may edit
    is the document owner's, decided like any other write to it.
    """
    if not may_access_artifact(artifact["parent_thread_id"], user_id, db=db):
        raise CommentActionError("not_found", "Comment not found")
    if actor_thread_id is None:
        return
    owner = artifact_owner(str(artifact["id"]), db=db) or artifact["parent_thread_id"]
    path = artifact.get("path")
    if path:
        from infra.config import WORKSPACE

        scope_root, conductor = document_scope(path, workspace=WORKSPACE, db=db)
        path_rel = scope_relative(Path(path).expanduser().resolve(strict=False), scope_root)
    else:
        conductor, path_rel = None, str(artifact.get("external_id") or artifact["id"])
    authority = may_edit_document(
        actor_thread_id, False, owner=owner, conductor=conductor, path_rel=path_rel,
        artifact_id=str(artifact["id"]), db=db,
    )
    if not authority.allowed:
        raise CommentActionError("not_owner", accept_refusal(owner_label(str(owner), db=db)))


_CONTENT_FIELDS = frozenset({"body", "anchor_text", "from_off", "to_off"})
_STATE_FIELDS = frozenset({"resolved", "acked"})


def check_agent_update(comment: dict[str, Any], fields: dict[str, Any], *, actor_thread_id: str, owner_thread_id: str | None, db) -> None:
    if comment.get("author_thread_id") == actor_thread_id:
        return
    if set(fields) & _CONTENT_FIELDS:
        raise CommentActionError("not_author", "an agent may only edit the content of comments it authored")
    if set(fields) & _STATE_FIELDS and actor_thread_id != owner_thread_id:
        owner = owner_row(db, owner_thread_id)
        _require_owner(actor_thread_id, owner_thread_id, owner[0] if owner else None, also="the comment's author can resolve, re-open or acknowledge it", instead="Reply to the comment instead.")
