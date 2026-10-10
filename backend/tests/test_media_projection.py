from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image


def _image_bytes(image_format: str) -> bytes:
    stream = BytesIO()
    Image.new("RGB", (2, 2), color=(31, 97, 173)).save(stream, format=image_format)
    return stream.getvalue()


@pytest.fixture(autouse=True)
def _canonical_thread_uploads(isolated_db, monkeypatch, tmp_path):
    from infra import config
    from infra.database import get_db

    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id, user_id, model, created_at, updated_at) "
            "VALUES ('thread-a', 'owner-a', 'claude-code/sonnet', 't', 't')"
        )
        db.commit()
    finally:
        db.close()


def _attachment(
    root,
    *,
    owner: str = "owner-a",
    file_id: str = "file-1",
    filename: str = "fact.png",
    mime_type: str = "image/png",
    data: bytes | None = None,
) -> dict:
    from registry import models

    payload = data if data is not None else _image_bytes("PNG")
    directory = root / ".threads" / "thread-a" / "uploads" / file_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_bytes(payload)
    models.register_artifact(
        integration="upload",
        type="upload",
        external_id=file_id,
        parent_thread_id="thread-a",
        path=str(path),
    )
    return {
        "file_id": file_id,
        "filename": filename,
        "mime_type": mime_type,
        "size": len(payload),
        "url": f"/api/uploads/{owner}/{file_id}/{filename}",
    }


def _target(*, harness: str = "native", supports_vision: bool = True):
    return SimpleNamespace(
        catalog_id=f"{harness}/vision",
        harness=harness,
        provider="openai",
        supports_vision=supports_vision,
    )


@pytest.mark.parametrize(
    ("image_format", "mime_type", "extension"),
    [
        ("PNG", "image/png", "png"),
        ("JPEG", "image/jpeg", "jpg"),
        ("GIF", "image/gif", "gif"),
        ("WEBP", "image/webp", "webp"),
    ],
)
def test_projection_accepts_supported_magic_and_preserves_fifo_order(
    tmp_path, image_format, mime_type, extension
):
    from core.media_projection import build_media_projection

    first = _attachment(
        tmp_path,
        file_id=f"first-{extension}",
        filename=f"first.{extension}",
        mime_type=mime_type,
        data=_image_bytes(image_format),
    )
    second = _attachment(
        tmp_path,
        file_id=f"second-{extension}",
        filename=f"second.{extension}",
        mime_type=mime_type,
        data=_image_bytes(image_format),
    )

    projection = build_media_projection(
        [first, second],
        owner_id="owner-a",
        thread_id="thread-a",
        target=_target(),

    )

    assert [image.attachment_id for image in projection.images] == [
        f"first-{extension}",
        f"second-{extension}",
    ]
    assert [image.mime_type for image in projection.images] == [mime_type, mime_type]
    assert all(
        image.sha256 and image.byte_size == len(image.data)
        for image in projection.images
    )


@pytest.mark.parametrize("harness", ["native", "codex"])
def test_projection_canonicalizes_image_jpg_for_a_valid_jpeg(tmp_path, harness):
    """A legacy JPEG MIME spelling reaches either vision harness as image/jpeg."""
    from core.media_projection import build_media_projection

    attachment = _attachment(
        tmp_path,
        file_id=f"plain-{harness}",
        filename="plain.jpeg",
        mime_type="image/jpg",
        data=_image_bytes("JPEG"),
    )

    projection = build_media_projection(
        [attachment],
        owner_id="owner-a",
        thread_id="thread-a",
        target=_target(harness=harness),
    )

    assert [image.mime_type for image in projection.images] == ["image/jpeg"]


@pytest.mark.parametrize(
    ("attachment", "cause"),
    [
        (
            {
                "file_id": "../owner-b",
                "filename": "x.png",
                "mime_type": "image/png",
            },
            "image_reference_unsafe",
        ),
        (
            {
                "file_id": "file-1",
                "filename": "/tmp/x.png",
                "mime_type": "image/png",
            },
            "image_reference_unsafe",
        ),
        (
            {
                "file_id": "file-1",
                "filename": "x.png",
                "mime_type": "image/png",
                "url": "/api/uploads/owner-b/file-1/x.png",
            },
            "image_owner_mismatch",
        ),
        (
            {
                "file_id": "missing",
                "filename": "x.png",
                "mime_type": "image/png",
            },
            "image_not_found",
        ),
    ],
)
def test_projection_rejects_unsafe_cross_owner_and_missing_references(
    tmp_path, attachment, cause
):
    from core.media_projection import MediaProjectionError, build_media_projection

    with pytest.raises(MediaProjectionError) as exc_info:
        build_media_projection(
            [attachment],
            owner_id="owner-a",
            thread_id="thread-a",
            target=_target(),

        )

    assert exc_info.value.cause == cause


def test_projection_accepts_a_mislabelled_image_and_refuses_only_undecodable_bytes(
    tmp_path,
):
    """Bytes are authoritative: a JPEG named .png is a usable photo, not an error.

    Delivery re-encodes from the bytes, so the declared container never decides
    what a provider receives and a mismatch cannot smuggle anything through.
    """
    from core.media_projection import MediaProjectionError, build_media_projection

    mismatch = _attachment(
        tmp_path,
        file_id="mismatch",
        filename="mismatch.png",
        mime_type="image/png",
        data=_image_bytes("JPEG"),
    )

    projection = build_media_projection(
        [mismatch],
        owner_id="owner-a",
        thread_id="thread-a",
        target=_target(),
    )

    assert [image.mime_type for image in projection.images] == ["image/jpeg"]

    corrupt = _attachment(
        tmp_path,
        file_id="corrupt",
        filename="corrupt.png",
        mime_type="image/png",
        data=b"not-an-image",
    )
    with pytest.raises(MediaProjectionError) as corrupt_error:
        build_media_projection(
            [corrupt],
            owner_id="owner-a",
            thread_id="thread-a",
            target=_target(),

        )
    assert corrupt_error.value.cause == "image_corrupt"


@pytest.mark.parametrize(
    ("image_format", "mime_type", "extension"),
    [
        ("TIFF", "image/tiff", "tif"),
        ("BMP", "image/bmp", "bmp"),
        ("PPM", "image/x-portable-pixmap", "ppm"),
        ("JPEG2000", "image/jp2", "jp2"),
    ],
)
def test_projection_admits_every_container_pillow_can_decode(
    tmp_path, image_format, mime_type, extension
):
    """The picker offers the user's photo, whatever container it happens to be in."""
    from core.media_projection import build_media_projection

    attachment = _attachment(
        tmp_path,
        file_id=f"as-{image_format}",
        filename=f"photo.{extension}",
        mime_type=mime_type,
        data=_image_bytes(image_format),
    )

    projection = build_media_projection(
        [attachment],
        owner_id="owner-a",
        thread_id="thread-a",
        target=_target(),
    )

    assert [image.mime_type for image in projection.images] == [mime_type]


def test_projection_declares_heif_decline_when_the_opener_is_absent(tmp_path, monkeypatch):
    """Without the optional HEIF package the user learns which capability is missing."""
    from core import image_resize
    from core.media_projection import MediaProjectionError, build_media_projection

    monkeypatch.setattr(image_resize, "_heif_state", "missing")
    heic = b"\x00\x00\x00\x18ftypmif1" + b"\x00" * 64
    attachment = _attachment(
        tmp_path,
        file_id="heic",
        filename="IMG_0001.heic",
        mime_type="image/heic",
        data=heic,
    )

    with pytest.raises(MediaProjectionError) as exc_info:
        build_media_projection(
            [attachment],
            owner_id="owner-a",
            thread_id="thread-a",
            target=_target(),
        )

    assert exc_info.value.cause == "image_heif_unavailable"
    assert "HEIF" in exc_info.value.detail


@pytest.mark.parametrize("image_format", ["PNG", "BMP"])
def test_projection_rejects_image_magic_misdeclared_as_non_image(
    tmp_path, image_format,
):
    from core.media_projection import MediaProjectionError, build_media_projection

    attachment = _attachment(
        tmp_path,
        file_id=f"misdeclared-{image_format}",
        filename="misdeclared.txt",
        mime_type="text/plain",
        data=_image_bytes(image_format),
    )

    with pytest.raises(MediaProjectionError) as exc_info:
        build_media_projection(
            [attachment],
            owner_id="owner-a",
            thread_id="thread-a",
            target=_target(harness="claude-code"),

        )

    assert exc_info.value.cause == "image_mime_mismatch"


def test_projection_uses_a_bounded_max_plus_one_read(tmp_path, monkeypatch):
    from pathlib import Path

    from core.media_projection import MediaProjectionLimits, build_media_projection

    payload = _image_bytes("PNG")
    attachment = _attachment(tmp_path, file_id="bounded", data=payload)
    real_open = Path.open
    read_sizes: list[int] = []

    class _ObservedReader:
        def __init__(self, path, *args, **kwargs):
            self._stream = real_open(path, *args, **kwargs)

        def __enter__(self):
            self._stream.__enter__()
            return self

        def __exit__(self, *args):
            return self._stream.__exit__(*args)

        def read(self, size: int = -1):
            read_sizes.append(size)
            return self._stream.read(size)

    monkeypatch.setattr(Path, "open", lambda path, *a, **k: _ObservedReader(path, *a, **k))
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda _path: pytest.fail("unbounded read_bytes must not be used"),
    )

    projection = build_media_projection(
        [attachment],
        owner_id="owner-a",
        thread_id="thread-a",
        target=_target(),

        limits=MediaProjectionLimits(max_image_bytes=len(payload)),
    )

    assert projection.images[0].data == payload
    assert read_sizes == [len(payload) + 1]


def test_projection_rejects_digest_change(tmp_path):
    from core.media_projection import MediaProjectionError, build_media_projection

    attachment = _attachment(tmp_path, file_id="stable")
    initial = build_media_projection(
        [attachment],
        owner_id="owner-a",
        thread_id="thread-a",
        target=_target(),

    )
    changed = _image_bytes("PNG") + b"changed-after-admission"
    (tmp_path / ".threads" / "thread-a" / "uploads" / "stable" / "fact.png").write_bytes(changed)

    with pytest.raises(MediaProjectionError) as exc_info:
        build_media_projection(
            [attachment],
            owner_id="owner-a",
            thread_id="thread-a",
            target=_target(),

            expected_digests=[initial.images[0].sha256],
        )

    assert exc_info.value.cause == "image_digest_changed"


@pytest.mark.parametrize(
    ("attachment_count", "limits", "cause"),
    [
        (1, {"max_image_bytes": 1}, "image_too_large"),
        (2, {"max_total_bytes": 1}, "image_total_too_large"),
        (2, {"max_count": 1}, "image_count_exceeded"),
    ],
)
def test_projection_enforces_per_image_total_and_count_limits(
    tmp_path, attachment_count, limits, cause
):
    from core.media_projection import (
        MediaProjectionError,
        MediaProjectionLimits,
        build_media_projection,
    )

    attachments = [
        _attachment(
            tmp_path,
            file_id=f"limit-{index}",
            filename=f"{index}.png",
        )
        for index in range(attachment_count)
    ]
    policy = MediaProjectionLimits(**limits)

    with pytest.raises(MediaProjectionError) as exc_info:
        build_media_projection(
            attachments,
            owner_id="owner-a",
            thread_id="thread-a",
            target=_target(),

            limits=policy,
        )

    assert exc_info.value.cause == cause


def test_projection_diagnostics_never_expose_sensitive_material(tmp_path):
    from core.media_projection import MediaProjectionError, build_media_projection

    sensitive_marker = "query-sensitive-marker"
    attachment = {
        "file_id": "../owner-b",
        "filename": "private.png",
        "mime_type": "image/png",
        "url": f"/api/uploads/owner-b/file/private.png?sig={sensitive_marker}",
        "raw_bytes": "bytes-sensitive-marker",
    }

    with pytest.raises(MediaProjectionError) as exc_info:
        build_media_projection(
            [attachment],
            owner_id="owner-a",
            thread_id="thread-a",
            target=_target(),

        )

    diagnostic = f"{exc_info.value!s} {exc_info.value!r}"
    assert str(tmp_path) not in diagnostic
    assert sensitive_marker not in diagnostic
    assert "bytes-sensitive-marker" not in diagnostic


def test_projection_rejects_non_vision_model_before_reading_upload(
    tmp_path, monkeypatch
):
    from pathlib import Path

    from core.media_projection import MediaProjectionError, build_media_projection

    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda _path: pytest.fail("upload read"),
    )

    with pytest.raises(MediaProjectionError) as exc_info:
        build_media_projection(
            [{
                "file_id": "file-1",
                "filename": "fact.png",
                "mime_type": "image/png",
            }],
            owner_id="owner-a",
            thread_id="thread-a",
            target=_target(supports_vision=False),

        )

    assert exc_info.value.cause == "image_model_unsupported"


def test_projection_names_svg_instead_of_calling_it_corrupt(tmp_path):
    """A valid SVG is unsupported, not damaged -- and the picker offers every file."""
    from core.media_projection import MediaProjectionError, build_media_projection

    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8">'
        b'<rect width="8" height="8" fill="red"/></svg>'
    )
    attachment = _attachment(
        tmp_path,
        file_id="vector",
        filename="logo.svg",
        mime_type="image/svg+xml",
        data=svg,
    )

    with pytest.raises(MediaProjectionError) as exc_info:
        build_media_projection(
            [attachment],
            owner_id="owner-a",
            thread_id="thread-a",
            target=_target(),
        )

    assert exc_info.value.cause == "image_corrupt"
    assert "SVG" in exc_info.value.detail
