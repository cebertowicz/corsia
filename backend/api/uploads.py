"""File upload and authenticated serving routes."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import BinaryIO

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from core.media_sniffing import IMAGE_SNIFF_BYTES
from core.workspace_layout import (
    resolve_project_root,
    resolve_thread_uploads_root,
    resolve_workspace_root,
)
from infra import config
from infra.auth import authenticate
from infra.config import (
    DOCUMENT_MIME_TYPES,
    MAX_IMAGE_UPLOAD_BYTES,
    MAX_UPLOAD_BYTES,
    MAX_VIDEO_UPLOAD_BYTES,
    UPLOADS_DIR,
    VIDEO_MIME_TYPES,
)
from infra.database import get_db
from infra.db_async import run_in_db
from infra.product_events import emit_product_event
from infra.queries import get_thread_for_user
from registry import models, service as registry_service

router = APIRouter(prefix="/api", tags=["uploads"])

_UPLOAD_CHUNK_BYTES = 1024 * 1024
_UPLOAD_SNIFF_BYTES = IMAGE_SNIFF_BYTES


class _UploadTooLarge(Exception):
    """A streamed upload crossed its detected media class's ceiling."""

    def __init__(self, limit: int):
        super().__init__(f"upload exceeds {limit} bytes")
        self.limit = limit


def upload_limits() -> dict[str, int]:
    """Return the server-owned upload ceilings clients must enforce."""
    return {
        "max_bytes": MAX_UPLOAD_BYTES,
        "max_image_bytes": MAX_IMAGE_UPLOAD_BYTES,
        "max_video_bytes": MAX_VIDEO_UPLOAD_BYTES,
    }


def _limit_for_detected_media(detected: tuple[str, str] | None) -> int:
    """Resolve the ceiling from server-inspected bytes, never declared MIME."""
    if detected is None:
        return MAX_UPLOAD_BYTES
    kind, _mime = detected
    if kind == "image":
        return MAX_IMAGE_UPLOAD_BYTES
    if kind == "video":
        return MAX_VIDEO_UPLOAD_BYTES
    return MAX_UPLOAD_BYTES


def _copy_bounded_upload(
    source: BinaryIO,
    destination: Path,
    filename: str,
) -> tuple[int, tuple[str, str] | None]:
    """Stream one spooled upload to disk under its detected byte ceiling."""
    from core.media_reads import detect_media

    source.seek(0)
    header = source.read(_UPLOAD_SNIFF_BYTES)
    detected = detect_media(header, Path(filename), source=source)
    limit = _limit_for_detected_media(detected)
    total = 0
    with destination.open("xb") as output:
        chunk = header
        while chunk:
            total += len(chunk)
            if total > limit:
                raise _UploadTooLarge(limit)
            output.write(chunk)
            chunk = source.read(_UPLOAD_CHUNK_BYTES)
    return total, detected


@router.get("/upload/limits")
async def get_upload_limits():
    """Publish upload ceilings so clients never hardcode their own."""
    return upload_limits()


def _thread_upload_root(thread_id: str, user_id: str) -> Path | None:
    """Return the authenticated owner's durable uploads root for one thread."""
    db = get_db()
    try:
        session = get_thread_for_user(thread_id, user_id, db=db)
        if session is None:
            return None
        project_id = session["project_id"]
        scope = (
            resolve_project_root(config.WORKSPACE, project_id, db=db)
            if project_id
            else resolve_workspace_root(config.WORKSPACE)
        )
        return resolve_thread_uploads_root(scope, thread_id) if scope else None
    finally:
        db.close()


def _canonical_upload_path(file_id: str, user_id: str, filename: str) -> Path | None:
    """Resolve a registered upload only when it stays in its owner's uploads root."""
    artifact = models.resolve("upload", "upload", file_id)
    if artifact is None or not artifact.get("path"):
        return None
    root = _thread_upload_root(artifact["parent_thread_id"], user_id)
    if root is None:
        return None
    candidate = Path(artifact["path"]).resolve(strict=False)
    canonical_root = root.resolve(strict=False)
    if (
        candidate.name != filename
        or not candidate.is_relative_to(canonical_root)
        or not candidate.is_file()
    ):
        return None
    return candidate


def _legacy_upload_path(user_id: str, file_id: str, filename: str) -> Path | None:
    """Resolve an existing global upload without permitting path escape."""
    root = (UPLOADS_DIR / user_id / file_id).resolve(strict=False)
    candidate = (root / filename).resolve(strict=False)
    if candidate.is_relative_to(root) and candidate.is_file():
        return candidate
    return None


@router.post("/upload")
async def upload_file(
    token: str | None,
    file: UploadFile = File(...),
    thread_id: str | None = None,
):
    """Store a thread-bound upload in its durable root and register its local path."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    if not thread_id:
        raise HTTPException(400, "thread_id is required")

    user_id = user["id"]
    file_id = str(uuid.uuid4())[:12]
    safe_name = Path(file.filename or "upload").name or "upload"
    root = _thread_upload_root(thread_id, user_id)
    if root is None:
        raise HTTPException(404, "Thread not found")
    dest_dir = root / file_id
    try:
        dest_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise HTTPException(500, "Could not allocate upload storage") from exc
    canonical_path = dest_dir / safe_name
    try:
        size, detected = await asyncio.to_thread(
            _copy_bounded_upload,
            file.file,
            canonical_path,
            safe_name,
        )
        mime = detected[1] if detected is not None else (
            file.content_type or "application/octet-stream"
        )
        if not canonical_path.resolve(strict=True).is_relative_to(root.resolve(strict=False)):
            raise ValueError("upload path escapes its thread uploads root")
        await run_in_db(
            registry_service.register_upload,
            thread_id,
            file_id=file_id,
            filename=safe_name,
            mime_type=mime,
            size=size,
            path=canonical_path,
        )
    except _UploadTooLarge as exc:
        canonical_path.unlink(missing_ok=True)
        dest_dir.rmdir()
        raise HTTPException(
            413,
            f"File too large. Max size is {exc.limit // (1024 * 1024)}MB.",
        ) from exc
    except Exception as exc:
        canonical_path.unlink(missing_ok=True)
        dest_dir.rmdir()
        raise HTTPException(500, "Could not register upload") from exc

    is_image = detected is not None and detected[0] == "image"
    is_document = mime in DOCUMENT_MIME_TYPES

    emit_product_event(
        "attachment", "added",
        user_id=user_id,
        properties={
            "file_id": file_id, "mime_type": mime,
            "size": size, "is_image": is_image,
            "is_document": is_document,
        },
    )

    return {
        "file_id": file_id,
        "filename": safe_name,
        "mime_type": mime,
        "size": size,
        "is_image": is_image,
        "is_document": is_document,
        "url": f"/api/uploads/{user_id}/{file_id}/{safe_name}",
    }


@router.get("/uploads/{user_id}/{file_id}/{filename}")
async def serve_upload(user_id: str, file_id: str, filename: str, token: str | None):
    """Serve a canonical registered upload, with safe legacy fallback."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    if user["id"] != user_id:
        raise HTTPException(403, "Access denied")

    path = _canonical_upload_path(file_id, user_id, filename)
    if path is None:
        path = _legacy_upload_path(user_id, file_id, filename)
    if path is None:
        raise HTTPException(404)
    return FileResponse(str(path))
