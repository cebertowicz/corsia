"""Canonical validated document load shared by HTTP and live WebSocket opens."""
import sqlite3
from pathlib import Path

from fastapi import HTTPException

from api.files_revision import _path_allowed, _writable
from api.files_roots import scope_bases
from doceditor.document_scope import MAX_TEXT_BYTES, revision
from infra.auth import owns_thread
from infra.database import get_db
from registry import service as registry_service
from registry.doc_freshness import artifact_freshness


def _live_open_setup(p: Path, project_id: str | None, thread_id: str | None, db) -> None:
    """Validated load calls the runtime's common identity setup.

    It lands before register-on-read can claim ownership. A failure refuses
    the open with a typed reason; opening again retries it.
    """
    from infra import config
    from infra.guidance_git import guidance_target
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


def read_document(path: str, user_id: str, project_id: str | None, thread_id: str | None,
                  *, live_open: bool = False) -> dict:
    """Validate one existing file, read its bytes, and resolve canonical identity."""
    db = get_db()
    try:
        if thread_id and not owns_thread(thread_id, user_id, db=db):
            raise HTTPException(404, "Thread not found")
        bases = scope_bases(project_id, db)
        try:
            p = Path(path).expanduser().resolve(strict=True)
        except OSError as exc:
            raise HTTPException(404, f"No such file: {exc}") from exc
        if not _path_allowed(p, bases, db=db):
            raise HTTPException(400, "Path is outside this scope's roots")
        writable = _writable(p, bases, db=db)
    finally:
        db.close()
    if not p.is_file():
        raise HTTPException(400, "Not a file")
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
