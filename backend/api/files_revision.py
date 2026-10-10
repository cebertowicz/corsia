"""Read-only content-revision endpoint for open artifact tabs.

Split from ``api/files.py`` so the change-signal surface has one home: this
module never writes, never registers a read to a thread, and never emits a
change notification.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from api.files_roots import scope_bases
from doceditor.document_scope import MAX_TEXT_BYTES, path_write_allowed, revision as _revision, stat_revision
from infra.auth import authenticate, may_access_artifact
from infra import config
from infra.database import get_db
from infra.db_async import run_in_db, run_off_db
from core.workspace_context_sources import is_workspace_context_path
from registry import models as registry_models
from registry import service as registry_service
from registry.file_types import image_media_type
from registry.doc_freshness import artifact_freshness

router = APIRouter(prefix="/api/files", tags=["files"])


def _path_allowed(path: Path, bases: list[Path], *, db=None) -> bool:
    return (
        any(path == b or b in path.parents for b in bases)
        or is_workspace_context_path(path, config.WORKSPACE, db=db)
    )


def _writable(path: Path, bases: list[Path], *, db=None) -> bool:
    """Whether the doc API would accept a write here.

    Mirrors the two refusals ``PUT /content`` enforces: workspace context files
    are read-only via this API, and everything must sit inside a write-allowed
    scope root. Reporting it on read lets the editor open a tab as ``readonly``
    instead of discovering it only when the first autosave fails.
    """
    if is_workspace_context_path(path, config.WORKSPACE, db=db):
        return False
    return path_write_allowed(path, bases)


@router.get("/revision")
async def revision(
    token: str,
    path: str,
    project_id: str | None = None,
    artifact_id: str | None = None,
):
    """Content revision and availability for an already-open document.

    Deliberately read-only: unlike ``GET /content`` it never registers the read
    to a thread and never emits a change notification, so an open editor tab can
    re-check its baseline (on a change nudge or when the window regains focus)
    without generating the very traffic it is watching for.

    ``artifact_id`` makes the registry the authority on *where* a resolved
    artifact currently lives: when the tab's remembered path is gone but the
    artifact still resolves elsewhere, the caller is told the new path so the
    tab relocates instead of going unavailable. A document that is genuinely
    gone reports ``exists: false`` rather than 404, so the tab can show its
    explicit unavailable state instead of a transport error.

    An image's revision comes from its size and modification time and its bytes are never read;
    a text document's is the sha256 of its bytes, hashed after the database connection is released.
    """
    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    def _locate():
        db = get_db()
        try:
            bases = scope_bases(project_id, db)
            found = _find_candidate(path, artifact_id, user["id"], bases, db)
            if found is None:
                return _unavailable()
            candidate, art = found
            resolved_artifact_id = (art or {}).get("id")
            st = candidate.stat()
            # An image is never opened as text, so its ceiling is the image one.
            media_type = image_media_type(candidate)
            if media_type and st.st_size > config.MAX_IMAGE_UPLOAD_BYTES:
                raise HTTPException(413, "Image too large to display")
            if not media_type and st.st_size > MAX_TEXT_BYTES:
                raise HTTPException(413, "File too large for the doc editor")
            return {
                "exists": True,
                "path": str(candidate),
                "revision": stat_revision(st) if media_type else None,
                "media_type": media_type,
                "size": st.st_size,
                "writable": _writable(candidate, bases, db=db),
                "artifact_id": resolved_artifact_id,
                "artifact": art,
                "freshness": artifact_freshness(art, candidate, db=db),
            }
        finally:
            db.close()

    located = await run_in_db(_locate)
    if located["exists"] and located["revision"] is None:
        try:
            located["revision"] = await run_off_db(_text_revision, Path(located["path"]))
        except OSError:
            return _unavailable()
    return located


def _find_candidate(path: str, artifact_id: str | None, user_id: str, bases: list[Path], db) -> tuple[Path, dict | None] | None:
    """The file a probe is about and its registry identity, or None when it is not available to this user."""
    if artifact_id:
        # A known identity controls both access and location. Do not fall
        # back to a remembered path, which could be stale or now unrelated.
        art = registry_models.get_artifact(artifact_id, db=db)
        if art is None or art["state"] != "active":
            return None
        if not may_access_artifact(art["parent_thread_id"], user_id, db=db):
            # A revision probe only serves an already-open tab. Returning the
            # same unavailable shape as a retired/missing identity avoids
            # revealing whether an inaccessible artifact id exists.
            return None
        candidate = _artifact_path(art, bases, db)
        return None if candidate is None else (candidate, art)
    try:
        candidate = Path(path).expanduser().resolve()
    except OSError:
        return None
    if not _path_allowed(candidate, bases, db=db):
        raise HTTPException(400, "Path is outside this scope's roots")
    if not candidate.is_file():
        return None
    return candidate, registry_service.resolve_file_identity(str(candidate), db=db)


def _text_revision(path: Path) -> str:
    return _revision(path.read_bytes())


def _unavailable() -> dict:
    """Canonical response for a previously open document that is no longer usable."""
    return {
        "exists": False, "path": None, "revision": None, "media_type": None,
        "size": None, "writable": False, "artifact_id": None,
        "artifact": None, "freshness": None,
    }


def _artifact_path(row: dict, bases: list[Path], db) -> Path | None:
    """Resolve the registry-authoritative current document path."""
    raw = row.get("path") or row.get("external_id")
    if not raw:
        return None
    try:
        candidate = Path(str(raw)).expanduser().resolve()
    except OSError:
        return None
    if not candidate.is_file() or not _path_allowed(candidate, bases, db=db):
        return None
    return candidate
