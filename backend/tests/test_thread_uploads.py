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


@pytest.mark.parametrize("filename,action", [
    ("archive.txt", "archived"),
    ("tombstone.txt", "tombstone"),
    ("image.png", "archived"),
    ("released.txt", "release"),
])
def test_retired_or_released_upload_is_not_a_file_only_card(
    main_db, filename, action,
):
    from infra import config
    from registry import models, ownership, service
    from registry.path_access import lookup_file
    from registry.path_cards import reference_cards

    workspace = config.WORKSPACE.resolve()
    path = workspace / ".threads" / "main-1" / "uploads" / "file-id" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"upload")
    artifact = service.register_upload(
        "main-1", file_id="file-id", filename=filename,
        mime_type="application/octet-stream", size=path.stat().st_size, path=path,
    )
    if action == "release":
        ownership.release_artifact(
            integration="upload", type=artifact["type"], external_id="file-id",
            actor_thread_id="main-1", authority="test", reason="test",
            source_path="test", db=main_db,
        )
    else:
        models.retire(artifact["id"], tombstone=action == "tombstone", db=main_db)

    found = lookup_file(str(path), [workspace], db=main_db)
    assert found.allowed and found.is_file and found.artifact is None
    assert found.has_identity
    assert reference_cards(main_db, str(path), [workspace], "steve", origin=workspace) == []


def test_upload_path_card_controls_and_active_precedence(main_db, monkeypatch):
    from infra import config
    from registry import models, registration_events, service
    from registry.path_access import lookup_file
    from registry.path_cards import reference_cards

    workspace = config.WORKSPACE.resolve()
    root = workspace / ".threads" / "main-1" / "uploads" / "controls"
    root.mkdir(parents=True, exist_ok=True)
    active = root / "active.txt"
    image = root / "active.png"
    plain = root / "plain.txt"
    for path in (active, image, plain):
        path.write_bytes(b"file")
    retired = service.register_upload(
        "main-1", file_id="old", filename=active.name,
        mime_type="application/octet-stream", size=4, path=active,
    )
    models.retire(retired["id"], db=main_db)
    current = service.register_upload(
        "main-1", file_id="current", filename=active.name,
        mime_type="application/octet-stream", size=4, path=active,
    )
    photo = service.register_upload(
        "main-1", file_id="photo", filename=image.name,
        mime_type="image/png", size=4, path=image,
    )
    calls = []
    original = registration_events.select_upload_path

    def counted(path, **kwargs):
        if not kwargs["active_only"]:
            calls.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(registration_events, "select_upload_path", counted)
    assert lookup_file(str(active), [workspace], db=main_db).artifact["id"] == current["id"]
    assert lookup_file(str(image), [workspace], db=main_db).artifact["id"] == photo["id"]
    assert calls == []
    found = lookup_file(str(plain), [workspace], db=main_db)
    assert found.allowed and not found.has_identity
    assert calls == [str(plain.resolve())]
    cards = reference_cards(main_db, str(plain), [workspace], "steve", origin=workspace)
    assert len(cards) == 1 and cards[0]["artifact_id"] is None
    assert not lookup_file(str(active), [workspace / "elsewhere"], db=main_db).allowed
    assert reference_cards(main_db, str(active), [workspace / "elsewhere"], "steve") == []
    calls.clear()
    memo = {}
    for _ in range(3):
        assert len(reference_cards(
            main_db, str(plain), [workspace], "steve", origin=workspace, lookup_memo=memo,
        )) == 1
    assert calls == [str(plain.resolve())]
    plan = main_db.execute(
        "EXPLAIN QUERY PLAN SELECT 1 FROM artifact_identities "
        "WHERE integration='upload' AND type IN (?, ?) AND path=? LIMIT 1",
        ("upload", "image", str(plain.resolve())),
    ).fetchall()
    assert any("idx_artifact_identities_upload_path" in row["detail"] for row in plan)


def test_inspect_upload_image_precedence_and_workspace_only_find(main_db):
    from infra import config
    from registry import models, service
    from tests.registry_helpers import seed_artifact

    path = (config.WORKSPACE / ".threads" / "main-1" / "uploads" / "choice" / "file.txt").resolve()
    for kind in ("upload", "image"):
        seed_artifact(main_db, artifact_id=kind, integration="upload", type=kind,
                      external_id=kind, parent_thread_id="main-1", path=str(path))
    main_db.commit()
    before = main_db._c.total_changes
    active, exists = service.inspect_file_identity(str(path), db=main_db)
    assert exists and active["id"] == "image"
    assert service.resolve_file_identity(str(path), db=main_db)["id"] == "image"
    assert service.find_file_identity(str(path), db=main_db) is None
    assert main_db._c.total_changes == before
    workspace_owner = service.register_write_strict("main-1", str(path), db=main_db)
    assert service.inspect_file_identity(str(path), db=main_db)[0]["id"] == workspace_owner["id"]
    models.retire(workspace_owner["id"], db=main_db)
    models.retire("image", db=main_db)
    assert service.inspect_file_identity(str(path), db=main_db)[0]["id"] == "upload"
    models.retire("upload", tombstone=True, db=main_db)
    before = main_db._c.total_changes
    assert service.inspect_file_identity(str(path), db=main_db) == (None, True)
    assert service.resolve_file_identity(str(path), db=main_db) is None
    assert service.find_file_identity(str(path), db=main_db)["id"] == workspace_owner["id"]
    assert main_db._c.total_changes == before


def test_inspect_selects_upload_route_once(main_db, monkeypatch):
    from infra import config
    from registry import file_identity_reads, service

    path = config.WORKSPACE / ".threads" / "main-1" / "uploads" / "route" / "missing.txt"
    original = file_identity_reads.uploads_root_for_path
    calls = []

    def counted(candidate):
        calls.append(candidate)
        return original(candidate)

    monkeypatch.setattr(file_identity_reads, "uploads_root_for_path", counted)
    assert service.inspect_file_identity(str(path), db=main_db) == (None, False)
    assert calls == [str(path.resolve())]


def test_workspace_path_miss_skips_upload_path_lookup(main_db, monkeypatch):
    """Workspace misses retain their logical-identity-only lookup semantics."""
    from infra import config
    from registry import registration_events, service

    path = config.WORKSPACE / "not-registered.md"
    path.write_text("not registered")
    monkeypatch.setattr(
        registration_events,
        "resolve_upload_path",
        lambda *_args, **_kwargs: pytest.fail("workspace miss queried uploads"),
    )

    assert service.resolve_file_identity(str(path), db=main_db) is None


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
