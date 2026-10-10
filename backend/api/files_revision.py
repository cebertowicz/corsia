"""Read-only content-revision endpoint for open artifact tabs.

Split from ``api/files.py`` so the change-signal surface has one home: this
module never writes, never registers a read to a thread, and never emits a
change notification.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from api.files_roots import scope_bases
from doceditor.document_scope import MAX_TEXT_BYTES, path_write_allowed, revision as _revision
from infra.auth import authenticate, may_access_artifact
from infra import config
from infra.database import get_db
from infra.db_async import run_in_db
from core.workspace_context_sources import is_workspace_context_path
from registry import models as registry_models
from registry import service as registry_service
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
    """
    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    def _revision_of():
        db = get_db()
        try:
            bases = scope_bases(project_id, db)
            if artifact_id:
                # A known identity controls both access and location. Do not fall
                # back to a remembered path, which could be stale or now unrelated.
                art = registry_models.get_artifact(artifact_id, db=db)
                if art is None or art["state"] != "active":
                    return _unavailable()
                if not may_access_artifact(art["parent_thread_id"], user["id"], db=db):
                    # A revision probe only serves an already-open tab. Returning the
                    # same unavailable shape as a retired/missing identity avoids
                    # revealing whether an inaccessible artifact id exists.
                    return _unavailable()
                candidate = _artifact_path(art, bases, db)
                if candidate is None:
                    return _unavailable()
                resolved_artifact_id = art["id"]
            else:
                try:
                    candidate = Path(path).expanduser().resolve()
                except OSError:
                    candidate = None
                if candidate is not None and not _path_allowed(candidate, bases, db=db):
                    raise HTTPException(400, "Path is outside this scope's roots")
                if candidate is None or not candidate.is_file():
                    return _unavailable()
                art = registry_service.resolve_file_identity(str(candidate), db=db)
                resolved_artifact_id = (art or {}).get("id")
            st = candidate.stat()
            if st.st_size > MAX_TEXT_BYTES:
                raise HTTPException(413, "File too large for the doc editor")
            raw = candidate.read_bytes()
            return {
                "exists": True,
                "path": str(candidate),
                "revision": _revision(raw),
                "size": st.st_size,
                "writable": _writable(candidate, bases, db=db),
                "artifact_id": resolved_artifact_id,
                "artifact": art,
                "freshness": artifact_freshness(art, candidate, db=db),
            }
        finally:
            db.close()

    return await run_in_db(_revision_of)


def _unavailable() -> dict:
    """Canonical response for a previously open document that is no longer usable."""
    return {
        "exists": False, "path": None, "revision": None,
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
