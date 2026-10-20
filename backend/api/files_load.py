"""Canonical validated document load shared by HTTP and live WebSocket opens."""
import sqlite3
from pathlib import Path

from fastapi import HTTPException

from api.files_revision import _writable
from api.files_roots import scope_bases
from doceditor.document_scope import MAX_TEXT_BYTES, revision
from infra import config
from infra.auth import owns_thread
from infra.database import get_db
from registry import service as registry_service
from registry.file_types import image_media_type
from registry.path_access import ReadRefused, checked_read
from registry.doc_freshness import artifact_freshness

#: Typed ``detail.code`` of the 415 a text open answers for an image file.
IMAGE_FILE_CODE = "image_file"


def _live_open_setup(p: Path, project_id: str | None, thread_id: str | None, db) -> None:
    """Validated load calls the runtime's common identity setup.

    It lands before register-on-read can claim ownership. A failure refuses
    the open with a typed reason; opening again retries it.
    """
    from infra import config
    from infra.guidance_targets import guidance_target
    from doceditor.document_scope import UI_THREAD
    from doceditor.live_admission import OpenRefused
    from doceditor.live_publication_record import WriteActor
    from doceditor.live_runtime import get_documents
    from registry.shared_write_errors import DurableWritePolicyError
    if guidance_target(p, config.WORKSPACE) is not None:
        return
    manager = get_documents()
    if manager is None:
        raise OpenRefused("service_unavailable", "Live document service is unavailable")
    actor = WriteActor(thread_id or UI_THREAD, project_id, direct_user=True)
    try:
        manager.setup_shared(p, actor, db=db)
    except DurableWritePolicyError:
        # A policy refusal is a decision, not a failed setup.
        raise
    except (OSError, sqlite3.Error) as error:
        raise OpenRefused("setup_failed", f"Document setup failed; open it again to retry ({error})") from error


def checked_read_path(path: str, project_id: str | None, db) -> tuple[Path, list[Path]]:
    """The read-scope gate (``checked_read``) for one project's roots, answered over HTTP.

    404 for a missing path, 400 for one outside the scope or not a file.
    """
    bases = scope_bases(project_id, db)
    try:
        return checked_read(path, bases, db=db), bases
    except ReadRefused as exc:
        if exc.reason == "missing":
            raise HTTPException(404, f"No such file: {exc}") from exc
        detail = "Path is outside this scope's roots" if exc.reason == "outside" else "Not a file"
        raise HTTPException(400, detail) from exc


def read_document(path: str, user_id: str, project_id: str | None, thread_id: str | None,
                  *, live_open: bool = False) -> dict:
    """Validate one existing file, read its bytes, and resolve canonical identity."""
    db = get_db()
    try:
        if thread_id and not owns_thread(thread_id, user_id, db=db):
            raise HTTPException(404, "Thread not found")
        p, bases = checked_read_path(path, project_id, db)
        writable = _writable(p, bases, db=db)
    finally:
        db.close()
    if image_media_type(p):
        # The viewer reads this code to open the file as an image instead; it is never opened as text.
        raise HTTPException(415, {"code": IMAGE_FILE_CODE, "message": "This file is an image"})
    st = p.stat()
    if st.st_size > MAX_TEXT_BYTES:
        raise HTTPException(413, "File too large for the doc editor")
    raw = p.read_bytes()
    if b"\x00" in raw[:8192]:
        raise HTTPException(415, "Binary file")
    text = raw.decode("utf-8", errors="replace")
    db = get_db()
    try:
        if live_open:
            _live_open_setup(p, project_id, thread_id, db)
        artifact_type = registry_service.classify(p)
        if thread_id:
            art = registry_service.register_write_strict(thread_id, str(p), db=db)
        else:
            art = registry_service.resolve_file_identity(str(p), db=db)
        freshness = artifact_freshness(art, p, db=db)
    finally:
        db.close()
    notification = art or {"integration": "workspace", "type": artifact_type, "external_id": str(p)}
    return {"text": text, "size": st.st_size, "mtime": st.st_mtime,
            "revision": revision(raw), "writable": writable, "artifact": art,
            "freshness": freshness, "_notification": notification}


def read_image_file(path: str, project_id: str | None) -> tuple[Path, str]:
    """Validate one workspace image for raw serving: scope check, then type and size.

    Read-only by design: serving bytes never registers the read or notifies.
    """
    db = get_db()
    try:
        p, _ = checked_read_path(path, project_id, db)
    finally:
        db.close()
    media_type = image_media_type(p)
    if media_type is None:
        raise HTTPException(415, "Not an image file")
    if p.stat().st_size > config.MAX_IMAGE_UPLOAD_BYTES:
        raise HTTPException(413, "Image too large to display")
    return p, media_type
