"""Canonical per-thread attachment storage and download contracts."""

from __future__ import annotations

import io

import pytest
from fastapi import HTTPException, UploadFile
from PIL import Image


def _png_bytes() -> bytes:
    image = io.BytesIO()
    Image.new("RGB", (2, 2), color="seagreen").save(image, format="PNG")
    return image.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["native", "codex"])
async def test_registered_thread_upload_projects_to_vision_targets(
    main_db,
    monkeypatch,
    tmp_path,
    harness,
):
    """Fresh canonical uploads reach every shared vision projection consumer."""
    from api import uploads
    from core.media_delivery import build_delivery_projection
    from core.media_projection import MediaProjectionError
    from infra import config

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    legacy_uploads = tmp_path / "legacy-uploads"
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(uploads, "authenticate", lambda _auth_input: {"id": "steve"})

    uploaded = await uploads.upload_file(
        None,
        UploadFile(
            filename="vision.png",
            file=io.BytesIO(_png_bytes()),
            headers={"content-type": "image/png"},
        ),
        thread_id="main-1",
    )

    target = type(
        "VisionTarget",
        (),
        {"harness": harness, "supports_vision": True},
    )()
    _public, projection = build_delivery_projection(
        [uploaded],
        owner_id="steve",
        thread_id="main-1",
        target=target,
    )

    assert projection.images[0].attachment_id == uploaded["file_id"]
    assert projection.images[0].data == _png_bytes()
    assert not legacy_uploads.exists()

    with pytest.raises(MediaProjectionError) as foreign_thread:
        build_delivery_projection(
            [uploaded],
            owner_id="steve",
            thread_id="another-thread",
            target=target,
        )
    assert foreign_thread.value.cause == "image_owner_mismatch"


@pytest.mark.asyncio
async def test_thread_upload_is_canonical_artifact_and_downloads_for_owner(
    main_db,
    monkeypatch,
    tmp_path,
):
    """A thread-bound upload has one durable local path and registry identity."""
    from api import uploads
    from infra import config
    from registry import models

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    legacy_uploads = tmp_path / "legacy-uploads"
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(uploads, "UPLOADS_DIR", legacy_uploads)
    monkeypatch.setattr(uploads, "authenticate", lambda _credential: {"id": "steve"})

    result = await uploads.upload_file(
        None,
        UploadFile(
            filename="report.txt",
            file=io.BytesIO(b"thread upload"),
            headers={"content-type": "text/plain"},
        ),
        thread_id="main-1",
    )

    canonical = (
        workspace / ".threads" / "main-1" / "uploads"
        / result["file_id"] / "report.txt"
    ).resolve()
    assert canonical.read_bytes() == b"thread upload"
    assert not (legacy_uploads / "steve" / result["file_id"] / "report.txt").exists()

    artifact = models.resolve("upload", "upload", result["file_id"])
    assert artifact is not None
    assert artifact["parent_thread_id"] == "main-1"
    assert artifact["path"] == str(canonical)
    assert artifact["retention"] == "durable"

    from registry import service

    service.register_uploads(
        "main-1",
        [result],
        source_input_id="accepted-upload",
    )
    assert models.resolve("upload", "upload", result["file_id"])["path"] == str(canonical)

    response = await uploads.serve_upload(
        "steve", result["file_id"], "report.txt", None,
    )
    assert response.path == str(canonical)


@pytest.mark.asyncio
async def test_upload_download_keeps_user_boundary_and_legacy_files_safe(
    main_db,
    monkeypatch,
    tmp_path,
):
    """Download rejects foreign access and never resolves a registered path outside its root."""
    from api import uploads
    from infra import config
    from registry import models

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    legacy_uploads = tmp_path / "legacy-uploads"
    legacy = legacy_uploads / "steve" / "legacy-id" / "old.txt"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy upload")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"must not serve")
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(uploads, "UPLOADS_DIR", legacy_uploads)
    monkeypatch.setattr(uploads, "authenticate", lambda _credential: {"id": "leksi"})

    with pytest.raises(HTTPException, match="Access denied") as denied:
        await uploads.serve_upload("steve", "legacy-id", "old.txt", None)
    assert denied.value.status_code == 403

    monkeypatch.setattr(uploads, "authenticate", lambda _credential: {"id": "steve"})
    legacy_response = await uploads.serve_upload("steve", "legacy-id", "old.txt", None)
    assert legacy_response.path == str(legacy)

    legacy_before = set(legacy_uploads.rglob("*"))
    with pytest.raises(HTTPException, match="thread_id is required") as missing_session:
        await uploads.upload_file(
            None,
            UploadFile(
                filename="new.txt",
                file=io.BytesIO(b"must be thread-bound"),
                headers={"content-type": "text/plain"},
            ),
        )
    assert missing_session.value.status_code == 400
    assert set(legacy_uploads.rglob("*")) == legacy_before

    models.register_artifact(
        integration="upload",
        type="upload",
        external_id="outside-id",
        parent_thread_id="main-1",
        path=str(outside),
    )
    with pytest.raises(HTTPException) as escaped:
        await uploads.serve_upload("steve", "outside-id", "outside.txt", None)
    assert escaped.value.status_code == 404


@pytest.mark.asyncio
async def test_agent_artifact_reference_includes_registered_upload_path(
    main_db,
    monkeypatch,
    tmp_path,
):
    """A real uploaded file reaches the provider-facing reference with its path."""
    from api import uploads
    from history.adapters import to_native_history
    from history.projection import render_portable_history
    from infra import config
    from infra.queries import insert_message

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(uploads, "authenticate", lambda _credential: {"id": "steve"})

    uploaded = await uploads.upload_file(
        None,
        UploadFile(
            filename="evidence.txt",
            file=io.BytesIO(b"upload reference"),
            headers={"content-type": "text/plain"},
        ),
        thread_id="main-1",
    )
    canonical = (
        workspace / ".threads" / "main-1" / "uploads"
        / uploaded["file_id"] / "evidence.txt"
    ).resolve()
    insert_message(
        "main-1",
        "user",
        "Read this file",
        metadata={
            "attachments": [uploaded],
            "turn_unbound_reason": "legacy_import",
        },
        db=main_db,
    )

    rendered = to_native_history(
        render_portable_history("main-1", db=main_db)
    )[0]["content"]

    assert str(canonical) in rendered
