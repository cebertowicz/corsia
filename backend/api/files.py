"""Documents file tree, content, write, and registration API."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.files_roots import (
    annotate,
    artifact_index,
    resolve_project,
    scope_bases,
    shared_projector,
    walk,
)
from doceditor.document_save import DocumentSave, DocumentSaveError, save_document_text
from doceditor.document_scope import UI_THREAD
from infra import config
from infra.auth import authenticate, owns_thread
from infra.database import get_db
from infra.db_async import run_in_db
from registry import service as registry_service
from registry.path_access import path_allowed

router = APIRouter(prefix="/api/files", tags=["files"])


@router.get("/tree")
async def tree(token: str, project_id: str | None = None):
    """The scope's doc roots, each a flat (pruned, capped) node list."""
    if not authenticate(token):
        raise HTTPException(401)

    def _tree():
        db = get_db()
        try:
            roots: list[dict] = []
            by_path = artifact_index(db)
            project = shared_projector(db)
            if project_id:
                planning, repos = resolve_project(project_id, db)
                if planning:
                    roots.append({"id": "planning", "label": "Project", "kind": "workspace", "path": str(planning)})
                for name, p in repos:
                    roots.append({"id": f"repo:{name}", "label": name, "kind": "repo", "path": str(p)})
            else:
                roots.append({"id": "ws", "label": "Workspace", "kind": "workspace", "path": str(config.WORKSPACE.resolve())})
            for r in roots:
                nodes, capped = walk(Path(r["path"]))
                annotate(nodes, Path(r["path"]), by_path, project)
                r["nodes"] = nodes
                r["capped"] = capped
            return {"roots": roots}
        finally:
            db.close()

    return await run_in_db(_tree)


@router.get("/content")
async def content(token: str, path: str, project_id: str | None = None, thread_id: str | None = None):
    """One file's text. With ``session_id``, registers the open to that thread."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    from api.files_load import read_document

    result = await run_in_db(read_document, path, user["id"], project_id, thread_id)
    notification = result.pop("_notification")
    if thread_id:
        # Outside the db worker: the WS nudge needs the running event loop.
        registry_service.notify_changed(notification)
    return result


class WriteBody(BaseModel):
    path: str
    text: str
    description: str | None = None
    project_id: str | None = None
    thread_id: str | None = None
    create: bool = False  # allow creating a new file (New document)


DOCUMENT_SAVE_HTTP_STATUS = {
    "project_not_found": 404,
    "thread_not_found": 404,
    "no_such_file": 404,
    "context_read_only": 403,
    "outside_scope": 400,
    "too_large": 413,
    "already_exists": 409,
    "not_a_file": 400,
    "invalid_skill": 422,
    "stale": 409,
    "document_live": 409,
    "store_damaged": 409,
    "store_unavailable": 409,
    "durable_write_policy": 403,
}


def document_save_http_exception(exc: DocumentSaveError) -> HTTPException:
    return HTTPException(
        DOCUMENT_SAVE_HTTP_STATUS[exc.kind],
        exc.detail if exc.detail is not None else exc.message,
    )


@router.put("/content")
async def write_content(token: str, body: WriteBody):
    """Persist the editor's autosave (plain overwrite — CRDT merge is a later
    layer; the doc editor is single-writer today). Registers the write to the
    session when one is in scope (register-on-write, registry §0.0 1.4)."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    try:
        return await save_document_text(DocumentSave(**body.model_dump()), user["id"])
    except DocumentSaveError as exc:
        raise document_save_http_exception(exc) from exc



class RegisterBody(BaseModel):
    path: str
    project_id: str | None = None
    thread_id: str | None = None


@router.post("/register")
async def register(token: str, body: RegisterBody):
    """Materialize an artifact row for a path (first-comment / explicit pin)."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    def _register():
        db = get_db()
        try:
            if body.thread_id and not owns_thread(body.thread_id, user["id"], db=db):
                raise HTTPException(404, "Thread not found")
            bases = scope_bases(body.project_id, db)
            try:
                p = Path(body.path).expanduser().resolve(strict=True)
            except OSError as e:
                raise HTTPException(404, f"No such file: {e}") from e
            if not path_allowed(p, bases, db=db):
                raise HTTPException(400, "Path is outside this scope's roots")
            row = registry_service.register_write_strict(
                body.thread_id or UI_THREAD,
                str(p),
                db=db,
            )
            return {
                "artifact": row,
                "_notification": row or {
                    "integration": "workspace",
                    "type": registry_service.classify(p),
                    "external_id": str(p),
                },
            }
        finally:
            db.close()

    result = await run_in_db(_register)
    notification = result.pop("_notification")
    if body.thread_id:
        registry_service.notify_changed(notification)
    return result
