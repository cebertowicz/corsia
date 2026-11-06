"""Document history behind the same scope gate as document opens: read versions, restore one."""
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from api.files_load import checked_read_path
from doceditor.document_save import DocumentSave, DocumentSaveError, save_document_text
from infra import config
from infra.auth import owns_thread
from infra.database import get_db
from infra.db_async import run_off_db
from infra.document_history import list_versions, restore_reason, version_bytes, version_text
from infra.guidance_git import GitFault

# Each restore refusal's status and detail; the client words its line from ``kind``.
_REFUSED = {
    "stale": (409, "The document changed after this version was opened. Nothing was restored."),
    "history_off": (409, "This document has no recorded history. Nothing was restored."),
    "version_unavailable": (404, "This version is not in the document's history. Nothing was restored."),
    "failed": (503, "History could not be recorded. Nothing was restored."),
}


def _resolve(path: str, user_id: str, project_id: str | None, thread_id: str | None):
    """The scope-checked file, its workspace-relative path and its guidance target."""
    db = get_db()
    try:
        if thread_id and not owns_thread(thread_id, user_id, db=db):
            raise HTTPException(404, "Thread not found")
        resolved, _ = checked_read_path(path, project_id, db)
    finally:
        db.close()
    try:
        relative = resolved.relative_to(config.WORKSPACE.resolve()).as_posix()
    except ValueError as exc:
        raise HTTPException(400, "Path is outside the workspace") from exc
    if relative.startswith("-"):
        raise HTTPException(400, "Invalid file path")
    from infra.guidance_targets import guidance_target

    return relative, guidance_target(resolved, config.WORKSPACE)


def history(path: str, user_id: str, project_id: str | None, thread_id: str | None, commit: str | None = None):
    relative, target = _resolve(path, user_id, project_id, thread_id)
    if target is None:
        if commit is not None:
            raise HTTPException(404, "Version not found")
        return {"versions": [], "truncated": False}
    if target.fault:
        raise HTTPException(503, "History is unavailable")
    try:
        if commit is None:
            versions, truncated = list_versions(config.WORKSPACE, relative)
            return {"versions": versions, "truncated": truncated}
        return {"text": version_text(config.WORKSPACE, relative, commit)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except GitFault as exc:
        raise HTTPException(503, "History is unavailable") from exc


def _restore_source(path: str, version: str, user_id: str, project_id: str | None,
                    thread_id: str | None) -> tuple[str, str] | str:
    """Blocking. The version's text and its server-built reason, or the refusal kind."""
    relative, target = _resolve(path, user_id, project_id, thread_id)
    if target is None:
        return "history_off"
    if target.fault:
        return "failed"
    workspace = Path(config.WORKSPACE)
    try:
        # A commit's bytes never change, so reading before the save's lock is safe.
        return version_bytes(workspace, relative, version).decode(), restore_reason(workspace, version)
    except ValueError:
        return "version_unavailable"
    except GitFault:
        return "failed"


def _refused(kind: str) -> JSONResponse:
    status, detail = _REFUSED[kind]
    return JSONResponse({"detail": detail, "kind": kind}, status_code=status)


async def restore(path: str, version: str, base_sha256: str, user_id: str, project_id: str | None,
                  thread_id: str | None):
    """Write ``version``'s text as a new recorded version, only while the file still hashes to ``base_sha256``."""
    source = await run_off_db(_restore_source, path, version, user_id, project_id, thread_id)
    if isinstance(source, str):
        return _refused(source)
    text, reason = source
    try:
        saved = await save_document_text(
            DocumentSave(path=path, text=text, project_id=project_id, thread_id=thread_id), user_id,
            reason=reason, expected_sha256=base_sha256, history_required=True,
        )
    except DocumentSaveError as exc:
        if exc.kind in {"stale", "history_off"}:
            return _refused(exc.kind)
        if exc.kind == "save_failed":
            return _refused("failed")
        from api.files import document_save_http_exception

        raise document_save_http_exception(exc) from exc
    return {"version": saved.get("version"), "sha256": saved["revision"]}
