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
            filename="report.png",
            file=io.BytesIO(_png_bytes()),
            headers={"content-type": "text/plain"},
        ),
        thread_id="main-1",
    )

    canonical = (
        workspace / ".threads" / "main-1" / "uploads"
        / result["file_id"] / "report.png"
    ).resolve()
    assert canonical.read_bytes() == _png_bytes()
    assert not (legacy_uploads / "steve" / result["file_id"] / "report.txt").exists()

    artifact = models.resolve_upload(result["file_id"])
    assert artifact is not None
    assert artifact["type"] == "image"
    assert artifact["parent_thread_id"] == "main-1"
    assert artifact["path"] == str(canonical)
    assert artifact["retention"] == "durable"

    from registry import service

    service.register_uploads(
        "main-1",
        [result],
        source_input_id="accepted-upload",
    )
    preserved = models.resolve_upload(result["file_id"])
    assert preserved is not None
    assert preserved["type"] == "image"
    assert preserved["path"] == str(canonical)
    assert main_db.execute(
        "SELECT COUNT(*) FROM artifact_identities WHERE integration='upload' AND external_id=?",
        (result["file_id"],),
    ).fetchone()[0] == 1

    response = await uploads.serve_upload(
        "steve", result["file_id"], "report.png", None,
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
            filename="evidence.png",
            file=io.BytesIO(_png_bytes()),
            headers={"content-type": "image/png"},
        ),
        thread_id="main-1",
    )
    canonical = (
        workspace / ".threads" / "main-1" / "uploads"
        / uploaded["file_id"] / "evidence.png"
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


def test_upload_identity_resolves_for_chat_cards_by_its_canonical_path(main_db):
    """A message link to a durable upload resolves its typed identity for a chat card."""
    from infra import config
    from registry import service

    path = config.WORKSPACE / ".threads" / "main-1" / "uploads" / "image-id" / "card.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_png_bytes())
    artifact = service.register_upload(
        "main-1", file_id="image-id", filename=path.name,
        mime_type="application/octet-stream", size=path.stat().st_size, path=path,
    )

    resolved = service.resolve_file_identity(str(path), db=main_db)

    assert resolved is not None
    assert resolved["id"] == artifact["id"]
    assert resolved["type"] == "image"


def test_register_upload_classifies_only_the_canonical_path_suffix(main_db, tmp_path):
    """Upload identity type follows its canonical file suffix, never MIME metadata."""
    from registry import models, service

    cases = [
        ("photo.png", "application/zip", "image"),
        ("archive.zip", "image/png", "upload"),
        ("no-suffix", "image/png", "upload"),
        ("diagram.svg", "application/octet-stream", "image"),
    ]
    for index, (filename, mime_type, expected_type) in enumerate(cases):
        path = tmp_path / filename
        path.write_bytes(b"upload")
        row = service.register_upload(
            "main-1",
            file_id=f"upload-{index}",
            filename=filename,
            mime_type=mime_type,
            size=path.stat().st_size,
            path=path,
        )

        assert row["type"] == expected_type
        resolved = models.resolve_upload(f"upload-{index}", db=main_db)
        assert resolved is not None
        assert resolved["type"] == expected_type


@pytest.mark.parametrize(
    ("archived_type", "active_type"),
    [
        pytest.param("image", "upload", id="archived-image-active-upload"),
        pytest.param("upload", "image", id="archived-upload-active-image"),
    ],
)
def test_resolve_upload_ignores_archived_twin(main_db, archived_type, active_type):
    """The active typed identity wins when a file id has an archived twin."""
    from registry import models

    file_id = f"twin-{archived_type}-{active_type}"
    archived = models.register_artifact(
        integration="upload",
        type=archived_type,
        external_id=file_id,
        parent_thread_id="archived-owner",
        db=main_db,
    )
    active = models.register_artifact(
        integration="upload",
        type=active_type,
        external_id=file_id,
        parent_thread_id="active-owner",
        db=main_db,
    )
    main_db.execute(
        "UPDATE artifact_identities SET state='archived' WHERE id=?",
        (archived["id"],),
    )
    main_db.commit()

    resolved = models.resolve_upload(file_id, db=main_db)

    assert resolved is not None
    assert resolved["id"] == active["id"]
    assert resolved["type"] == active_type
