"""Document history behind the same scope gate as document opens: read versions, restore one."""
from pathlib import Path

from fastapi import HTTPException
from loguru import logger
from fastapi.responses import JSONResponse

from api.files_load import checked_read_path
from doceditor.document_save import DocumentSave, DocumentSaveError, save_document_text
from infra import config
from infra.auth import owns_thread
from infra.database import get_db
from infra.db_async import run_off_db
from infra.document_history import list_versions, repository, restore_reason, version_bytes, version_text
from infra.document_recorder import wait_recorded
from infra.guidance_git import GitFault

# Each restore refusal's status and detail; the client words its line from ``kind``. Every refusal that
# retrying cannot change carries its own kind, so the surface never offers a Retry that only repeats it.
_REFUSED = {
    "stale": (409, "The document changed after this version was opened. Nothing was restored."),
    "history_off": (409, "This document has no recorded history. Nothing was restored."),
    "version_unavailable": (404, "This version is not in the document's history. Nothing was restored."),
    "failed": (503, "History could not be recorded. Nothing was restored."),
    "invalid_skill": (422, "This version's text is not a valid skill. Nothing was restored."),
    "context_read_only": (403, "This file is read-only. Nothing was restored."),
}


def _resolve(path: str, user_id: str, project_id: str | None, thread_id: str | None):
    """The scope-checked file's workspace-relative path, its history target and the file itself."""
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
    from infra.guidance_targets import history_target

    return relative, history_target(resolved, config.WORKSPACE), resolved


def history(path: str, user_id: str, project_id: str | None, thread_id: str | None, commit: str | None = None):
    relative, target, _file = _resolve(path, user_id, project_id, thread_id)
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
        if (repo := repository(config.WORKSPACE)) is None:
            raise HTTPException(404, "Version not found")
        return {"text": version_text(repo, relative, commit)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except GitFault as exc:
        raise HTTPException(503, "History is unavailable") from exc


def _restore_source(path: str, version: str, user_id: str, project_id: str | None,
                    thread_id: str | None) -> tuple[str, str, Path] | str:
    """Blocking. The version's text, its server-built reason and the file, or the refusal kind."""
    relative, target, resolved = _resolve(path, user_id, project_id, thread_id)
    if target is None:
        return "history_off"
    if target.fault:
        return "failed"
    try:
        # One repository lookup serves the whole read.
        if (repo := repository(Path(config.WORKSPACE))) is None:
            return "history_off"
        # A commit's bytes never change, so reading before the save's lock is safe.
        return version_bytes(repo, relative, version).decode(), restore_reason(repo, version), resolved
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
    text, reason, resolved = source
    from doceditor.live_tools import find_live

    if (entry := find_live(resolved)) is not None:
        # An open document changes only through its live lane, which records the version.
        return await _restore_live(entry, resolved, text, reason, base_sha256, user_id, project_id, thread_id)
    try:
        saved = await save_document_text(
            DocumentSave(path=path, text=text, project_id=project_id, thread_id=thread_id), user_id,
            reason=reason, expected_sha256=base_sha256, history_required=True,
        )
    except DocumentSaveError as exc:
        if exc.kind in _REFUSED:
            return _refused(exc.kind)
        if exc.kind == "save_failed":
            return _refused("failed")
        from api.files import document_save_http_exception

        raise document_save_http_exception(exc) from exc
    return {"version": saved.get("version"), "sha256": saved["revision"]}


async def _restore_live(entry, resolved: Path, text: str, reason: str, base_sha256: str, user_id: str,
                        project_id: str | None, thread_id: str | None):
    """Replace an open document's text with the version's, as the person, only while it still holds the
    text they restored from. The lane writes it and queues its version, which is recorded before this
    answers, so the history the person reloads holds the restore; its version id is not known here."""
    import hashlib

    from doceditor.document_scope import UI_THREAD
    from doceditor.live_operation import Operation
    from doceditor.live_publication_record import WriteActor

    actor = WriteActor(thread_id or UI_THREAD, project_id, direct_user=True, user_id=user_id)
    try:
        result = await entry.lane.restore(Operation("replace", content=text, actor=actor, reason=reason), base_sha256)
        result.require_landed()
    except DocumentSaveError as exc:
        return _refused(exc.kind if exc.kind in _REFUSED else "failed")
    except Exception:
        logger.opt(exception=True).warning("live restore of {} refused", entry.artifact_id)
        return _refused("failed")
    if not result.server_saved:
        return _refused("failed")
    await run_off_db(wait_recorded, {resolved})
    return {"version": None, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
