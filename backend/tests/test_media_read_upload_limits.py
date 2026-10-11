"""Server-owned image, video, and generic upload-limit contracts."""

from __future__ import annotations

import json
from io import BytesIO

import pytest
from PIL import Image


def _png_bytes() -> bytes:
    stream = BytesIO()
    Image.new("RGB", (4, 4), color="red").save(stream, format="PNG")
    return stream.getvalue()


def _bmp_bytes() -> bytes:
    stream = BytesIO()
    Image.new("RGB", (4, 4), color="blue").save(stream, format="BMP")
    return stream.getvalue()


def _pillow_image_bytes(
    image_format: str,
    size: tuple[int, int] = (16, 16),
) -> bytes:
    stream = BytesIO()
    mode = "1" if image_format == "XBM" else "RGB"
    Image.new(mode, size, color=1 if mode == "1" else "green").save(
        stream,
        format=image_format,
    )
    return stream.getvalue()


_PILLOW_FALLBACK_CASES = [
    ("JPEG2000", "scan.jp2", "image/jp2"),
    ("PPM", "scan.ppm", "image/x-portable-pixmap"),
    ("ICO", "icon.ico", "image/vnd.microsoft.icon"),
    ("SGI", "render.sgi", "image/sgi"),
    ("TGA", "render.tga", "image/x-tga"),
    ("XBM", "mask.xbm", "image/x-xbitmap"),
]


def test_image_and_video_limits_are_separate_from_other_upload_consumers() -> None:
    """Image 50MB and video 500MB policy is distinct from the generic limit."""
    from core.media_projection import MediaProjectionLimits
    from infra.config import (
        MAX_IMAGE_UPLOAD_BYTES,
        MAX_UPLOAD_BYTES,
        MAX_VIDEO_UPLOAD_BYTES,
        VIDEO_MIME_TYPES,
    )

    assert MAX_IMAGE_UPLOAD_BYTES == 50 * 1024 * 1024
    assert MAX_VIDEO_UPLOAD_BYTES == 500 * 1024 * 1024
    assert MAX_UPLOAD_BYTES < MAX_IMAGE_UPLOAD_BYTES
    assert MediaProjectionLimits().max_image_bytes == MAX_IMAGE_UPLOAD_BYTES
    assert "video/mp4" in VIDEO_MIME_TYPES


def test_upload_route_publishes_server_owned_limits() -> None:
    """The frontend reads limits from the server instead of hardcoding them."""
    from api.uploads import upload_limits
    from infra.config import (
        MAX_IMAGE_UPLOAD_BYTES,
        MAX_UPLOAD_BYTES,
        MAX_VIDEO_UPLOAD_BYTES,
    )

    limits = upload_limits()

    assert limits["max_bytes"] == MAX_UPLOAD_BYTES
    assert limits["max_image_bytes"] == MAX_IMAGE_UPLOAD_BYTES
    assert limits["max_video_bytes"] == MAX_VIDEO_UPLOAD_BYTES


def test_upload_copy_uses_detected_bytes_and_bounded_reads(tmp_path) -> None:
    """A spoofed media MIME cannot widen policy, and copying stays bounded."""
    from io import BytesIO

    from api.uploads import (
        _UPLOAD_CHUNK_BYTES,
        _UploadTooLarge,
        _copy_bounded_upload,
    )
    from infra.config import MAX_UPLOAD_BYTES

    class TrackingBytesIO(BytesIO):
        requested_sizes: list[int]

        def __init__(self, value: bytes):
            super().__init__(value)
            self.requested_sizes = []

        def read(self, size: int = -1) -> bytes:
            self.requested_sizes.append(size)
            return super().read(size)

    source = TrackingBytesIO(b"x" * (MAX_UPLOAD_BYTES + 1))
    destination = tmp_path / "spoof.mp4"

    with pytest.raises(_UploadTooLarge) as raised:
        _copy_bounded_upload(source, destination, "spoof.mp4")

    assert raised.value.limit == MAX_UPLOAD_BYTES
    assert max(source.requested_sizes) <= _UPLOAD_CHUNK_BYTES
    assert destination.stat().st_size <= MAX_UPLOAD_BYTES


@pytest.mark.asyncio
async def test_upload_records_detected_media_mime_not_client_declaration(
    main_db, monkeypatch, tmp_path,
) -> None:
    """Admission and durable metadata share the server-proven media class."""
    from fastapi import UploadFile

    from api import uploads
    from infra import config
    from registry import models

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(uploads, "authenticate", lambda _credential: {"id": "steve"})

    result = await uploads.upload_file(
        None,
        UploadFile(
            filename="misdeclared.txt",
            file=BytesIO(_png_bytes()),
            headers={"content-type": "text/plain"},
        ),
        thread_id="main-1",
    )

    artifact = models.resolve_upload(result["file_id"])
    assert result["mime_type"] == "image/png"
    assert result["is_image"] is True
    assert artifact is not None
    assert json.loads(artifact["metadata"])["mime"] == "image/png"


@pytest.mark.asyncio
async def test_bm_prefixed_text_upload_remains_text(
    main_db, monkeypatch, tmp_path,
) -> None:
    """Ordinary BM-prefixed text keeps its declared MIME and file semantics."""
    from fastapi import UploadFile
    from starlette.datastructures import Headers

    from api import uploads
    from infra import config

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(uploads, "authenticate", lambda _credential: {"id": "steve"})

    result = await uploads.upload_file(
        None,
        UploadFile(
            filename="ranking.txt",
            file=BytesIO(b"BM25 ranking notes\n"),
            headers=Headers({"content-type": "text/plain"}),
        ),
        thread_id="main-1",
    )

    assert result["mime_type"] == "text/plain"
    assert result["is_image"] is False


def _ftyp(brand: bytes) -> bytes:
    """An ISO base media header whose major brand is ``brand``."""
    return b"\x00\x00\x00\x18ftyp" + brand + b"\x00\x00\x00\x00"


@pytest.mark.parametrize(
    ("header", "mime"),
    [
        (_ftyp(b"heic"), "image/heic"),
        (_ftyp(b"heix"), "image/heic"),
        (_ftyp(b"hevc"), "image/heic-sequence"),
        (_ftyp(b"mif1"), "image/heif"),
        (_ftyp(b"msf1"), "image/heif-sequence"),
        (b"II*\x00\x08\x00\x00\x00", "image/tiff"),
        (b"MM\x00*\x00\x00\x00\x08", "image/tiff"),
        (_bmp_bytes(), "image/bmp"),
        (_ftyp(b"isom"), None),
    ],
)
def test_sniffer_proves_every_admitted_image_container(
    header, mime,
) -> None:
    """The sniffer knows every container image validation decodes as a photo."""
    from core.media_sniffing import image_mime_from_prefix

    assert image_mime_from_prefix(header) == mime


@pytest.mark.parametrize(
    "payload",
    [
        b"BM25 ranking notes",
        b"BMI,age,weight\n",
        b"BMW maintenance log",
    ],
)
def test_bm_prefixed_text_is_not_classified_as_bmp(payload) -> None:
    """Two leading letters cannot prove that ordinary text is a BMP image."""
    from core.media_sniffing import image_mime_from_prefix

    assert image_mime_from_prefix(payload) is None


@pytest.mark.parametrize(
    ("start", "replacement"),
    [
        (2, (17).to_bytes(4, "little")),
        (6, b"\x01\x00\x00\x00"),
        (10, (14).to_bytes(4, "little")),
        (14, (13).to_bytes(4, "little")),
    ],
)
def test_bmp_sniffer_rejects_malformed_file_header(start, replacement) -> None:
    """Size, reserved fields, pixel offset, and DIB size must agree."""
    from core.media_sniffing import image_mime_from_prefix

    payload = bytearray(_bmp_bytes())
    payload[start : start + 4] = replacement

    assert image_mime_from_prefix(bytes(payload)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "data", "expected_mime"),
    [
        ("photo.heic", _ftyp(b"heic"), "image/heic"),
        ("photo.heif", _ftyp(b"mif1"), "image/heif"),
        ("scan.tiff", b"II*\x00\x08\x00\x00\x00", "image/tiff"),
        ("drawing.bmp", _bmp_bytes(), "image/bmp"),
    ],
)
async def test_admitted_image_uploads_are_reported_as_images(
    main_db, monkeypatch, tmp_path, filename, data, expected_mime,
) -> None:
    """The response preserves byte-proven image identity for every container."""
    from fastapi import UploadFile

    from api import uploads
    from infra import config

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(uploads, "authenticate", lambda _value: {"id": "steve"})

    result = await uploads.upload_file(
        None,
        UploadFile(filename=filename, file=BytesIO(data)),
        thread_id="main-1",
    )

    assert result["mime_type"] == expected_mime
    assert result["is_image"] is True


@pytest.mark.parametrize(
    ("image_format", "filename", "expected_mime"),
    _PILLOW_FALLBACK_CASES,
)
def test_full_stream_detection_matches_delivery_image_identity(
    image_format, filename, expected_mime,
) -> None:
    """The media owner recognizes every container Pillow delivery accepts."""
    from pathlib import Path

    from core.media_reads import detect_media
    from core.media_sniffing import IMAGE_SNIFF_BYTES

    source = BytesIO(_pillow_image_bytes(image_format))
    header = source.read(IMAGE_SNIFF_BYTES)
    position = source.tell()

    assert detect_media(header, Path(filename), source=source) == (
        "image",
        expected_mime,
    )
    assert source.tell() == position


def test_full_stream_detection_declines_decompression_bomb() -> None:
    """An implausible fallback-format header stays a controlled non-media result."""
    from pathlib import Path

    from core.media_reads import detect_media
    from core.media_sniffing import IMAGE_SNIFF_BYTES

    source = BytesIO(b"P6\n20000 20000\n255\n")
    header = source.read(IMAGE_SNIFF_BYTES)
    position = source.tell()

    assert detect_media(header, Path("hostile.ppm"), source=source) is None
    assert source.tell() == position


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("image_format", "filename", "expected_mime"),
    _PILLOW_FALLBACK_CASES,
)
async def test_pillow_decodable_uploads_share_delivery_image_identity(
    main_db, monkeypatch, tmp_path, image_format, filename, expected_mime,
) -> None:
    """A valid image cannot be file-typed at upload then image-typed at delivery."""
    from fastapi import UploadFile
    from starlette.datastructures import Headers

    from api import uploads
    from infra import config

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(uploads, "authenticate", lambda _value: {"id": "steve"})

    result = await uploads.upload_file(
        None,
        UploadFile(
            filename=filename,
            file=BytesIO(_pillow_image_bytes(image_format)),
            headers=Headers({"content-type": expected_mime}),
        ),
        thread_id="main-1",
    )

    assert result["mime_type"] == expected_mime
    assert result["is_image"] is True


def test_non_prefix_image_above_generic_limit_gets_the_image_ceiling(
    tmp_path,
) -> None:
    """Full-stream proof selects the image ceiling before copy begins."""
    from api.uploads import _copy_bounded_upload
    from infra.config import MAX_UPLOAD_BYTES

    data = _pillow_image_bytes("PPM", size=(1900, 1900))
    assert len(data) > MAX_UPLOAD_BYTES

    copied, detected = _copy_bounded_upload(
        BytesIO(data),
        tmp_path / "scan.ppm",
        "scan.ppm",
    )

    assert copied == len(data)
    assert detected == ("image", "image/x-portable-pixmap")


@pytest.mark.parametrize(
    ("header", "filename", "mime"),
    [
        (_ftyp(b"heic"), "photo.heic", "image/heic"),
        (b"II*\x00\x08\x00\x00\x00", "scan.tiff", "image/tiff"),
        (_bmp_bytes(), "drawing.bmp", "image/bmp"),
    ],
)
def test_detected_image_uploads_get_the_image_ceiling(
    tmp_path, header, filename, mime,
) -> None:
    """A photo above the generic limit is admitted under the image limit."""
    from api.uploads import _copy_bounded_upload
    from infra.config import MAX_UPLOAD_BYTES

    size = MAX_UPLOAD_BYTES + 1
    source = BytesIO(header + b"\x00" * (size - len(header)))

    copied, detected = _copy_bounded_upload(source, tmp_path / filename, filename)

    assert copied == size
    assert detected == ("image", mime)


@pytest.mark.asyncio
async def test_heic_photo_above_the_generic_limit_uploads_as_heic(
    main_db, monkeypatch, tmp_path,
) -> None:
    """A phone photo between the generic and image limits is admitted as HEIC."""
    from fastapi import UploadFile

    from api import uploads
    from infra import config

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(uploads, "authenticate", lambda _credential: {"id": "steve"})
    header = _ftyp(b"heic")
    size = config.MAX_UPLOAD_BYTES + 1

    result = await uploads.upload_file(
        None,
        UploadFile(
            filename="IMG_0001.HEIC",
            file=BytesIO(header + b"\x00" * (size - len(header))),
        ),
        thread_id="main-1",
    )

    assert result["mime_type"] == "image/heic"
    assert result["size"] == size
    assert result["is_image"] is True
