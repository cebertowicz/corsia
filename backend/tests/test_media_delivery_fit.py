"""Delivery fits validated uploads per target without touching source identity."""

from __future__ import annotations

import asyncio
import os
import threading
from hashlib import sha256
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

_THREAD_ID = "fit-thread"
_OWNER_ID = "owner"
_TINY_LIMITS = {"max_bytes": 20_000, "max_dimension": 100}


def _noise_png(size: int) -> bytes:
    """Return a real PNG that cannot fit a tiny provider limit unchanged."""
    stream = BytesIO()
    Image.frombytes("RGB", (size, size), os.urandom(size * size * 3)).save(
        stream, format="PNG"
    )
    return stream.getvalue()


def _target(harness: str, provider: str) -> SimpleNamespace:
    return SimpleNamespace(
        catalog_id=f"{harness}/vision",
        harness=harness,
        provider=provider,
        supports_vision=True,
    )


@pytest.fixture
def oversized_upload(isolated_db, monkeypatch, tmp_path):
    """Register one thread upload larger than the tiny provider limits."""
    from infra import config
    from registry import models
    from shared_send_fixtures import _insert_session

    _insert_session(_THREAD_ID, user_id=_OWNER_ID, spawned_by=None)
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    for provider in ("anthropic", "openai"):
        monkeypatch.setitem(config.PROVIDER_IMAGE_LIMITS, provider, _TINY_LIMITS)
    source = _noise_png(300)
    path = tmp_path / ".threads" / _THREAD_ID / "uploads" / "big" / "big.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(source)
    models.register_artifact(
        integration="upload",
        type="upload",
        external_id="big",
        parent_thread_id=_THREAD_ID,
        path=str(path),
    )
    attachment = {
        "file_id": "big",
        "filename": "big.png",
        "mime_type": "image/png",
        "size": len(source),
        "url": f"/api/uploads/{_OWNER_ID}/big/big.png",
    }
    return attachment, path, source


def _deliver(attachments, target, *, trusted: bool = False):
    from core.media_delivery import build_delivery_projection

    return build_delivery_projection(
        attachments,
        owner_id=_OWNER_ID,
        thread_id=_THREAD_ID,
        target=target,
        trust_descriptor_digests=trusted,
    )


@pytest.mark.parametrize(
    ("harness", "provider"),
    [("claude-code", "unused"), ("codex", "unused"), ("native", "openai")],
)
def test_delivery_fits_every_harness_and_keeps_the_source(
    oversized_upload, harness, provider,
):
    attachment, path, source = oversized_upload

    _public, projection = _deliver([attachment], _target(harness, provider))

    image = projection.images[0]
    assert image.byte_size <= _TINY_LIMITS["max_bytes"]
    with Image.open(BytesIO(image.data)) as delivered:
        assert max(delivered.size) <= _TINY_LIMITS["max_dimension"]
    assert image.sha256 == sha256(image.data).hexdigest()
    assert projection.digests == (sha256(source).hexdigest(),)
    assert path.read_bytes() == source


def test_admitted_oversized_image_redelivers_against_its_source_identity(
    oversized_upload,
):
    """Admission binds the source digest, so a fitted image is not 'changed'."""
    from core.media_delivery import bind_projection_to_descriptor

    attachment, _path, source = oversized_upload
    target = _target("claude-code", "unused")
    public, admitted = _deliver([attachment], target)
    descriptor = bind_projection_to_descriptor(public, admitted)

    assert descriptor[0]["_corsia_validated_image_sha256"] == sha256(source).hexdigest()

    _public, redelivered = _deliver(descriptor, target, trusted=True)

    assert redelivered.images[0].byte_size <= _TINY_LIMITS["max_bytes"]
    assert redelivered.digests == admitted.digests


def test_switching_to_an_unlimited_target_refits_from_full_resolution(
    oversized_upload,
):
    from core.media_delivery import bind_projection_to_descriptor

    attachment, _path, source = oversized_upload
    public, admitted = _deliver([attachment], _target("codex", "unused"))
    descriptor = bind_projection_to_descriptor(public, admitted)

    _public, switched = _deliver(descriptor, _target("native", "omlx"), trusted=True)

    assert switched.images[0].data == source


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module_name",
    ["subagents.codex_inbox_batch", "subagents.cc_runtime_steering"],
)
async def test_live_steer_projection_runs_off_the_event_loop(
    monkeypatch, module_name,
):
    """Fitting may decode pixels and wait for a slot; the loop never blocks on it."""
    import importlib

    from infra import db_async
    from infra.thread_inputs import ClaimedBatch, ClaimedInput
    from unittest.mock import AsyncMock

    module = importlib.import_module(module_name)
    # A module first imported while another test patched infra.db_async
    # keeps that inline stand-in; assert against the real DB executor.
    monkeypatch.setattr("infra.input_render.run_in_db", db_async.run_in_db)
    monkeypatch.setattr("infra.preflight_attempt.live_input_fits", AsyncMock(return_value=True))
    loop_thread = threading.get_ident()
    projection_threads: list[int] = []
    projection = SimpleNamespace(digests=("image",))

    def fit_projection():
        projection_threads.append(threading.get_ident())
        return [], projection

    async def fake_projection(*_args, **_kwargs):
        return await asyncio.to_thread(fit_projection)

    monkeypatch.setattr(
        "core.media_delivery.build_delivery_projection_async", fake_projection,
    )
    batch = ClaimedBatch(
        _THREAD_ID, "turn", 1, 0, "selected", (ClaimedInput(
            "input", 1, 0, "user", "inspect", "apply_now", {},
            {"attachments": [{"file_id": "x"}]}, "t0",
        ),),
    )

    result = await module.prepare_live_input(batch, object(), {"user_id": _OWNER_ID}, object())

    assert result is not None and result[1].media_projection is projection
    assert projection_threads and projection_threads[0] != loop_thread
    assert asyncio.get_running_loop().is_running()


@pytest.mark.asyncio
async def test_send_path_image_fit_does_not_run_on_the_shared_db_pool(
    monkeypatch, oversized_upload,
):
    """The CPU-bound fit must not run on infra.db_async's shared pool.

    ``_bind_image_identity`` used to wrap the whole projection build
    (DB-backed validation and the semaphore-gated fit together) in one
    ``run_in_db`` call, so the fit ran on a ``db``-prefixed worker thread --
    the same pool every unrelated database operation shares. Exhausting the
    fit's two-slot semaphore then held that worker hostage and starved
    unrelated DB work behind it (issue #1659).
    """
    from core import image_resize
    from infra import send_service

    attachment, _path, _source = oversized_upload
    target = _target("claude-code", "unused")

    fit_threads: list[str] = []
    real_resize = image_resize.resize_image_for_provider

    def spying_resize(*args, **kwargs):
        fit_threads.append(threading.current_thread().name)
        return real_resize(*args, **kwargs)

    monkeypatch.setattr(image_resize, "resize_image_for_provider", spying_resize)

    result = await send_service._bind_image_identity(
        _THREAD_ID, {"user_id": _OWNER_ID, "model": None}, [attachment], target,
    )

    assert result and result[0]["_corsia_validated_image_sha256"]
    assert fit_threads, "expected the fit to actually run"
    assert not any(name.startswith("db") for name in fit_threads), (
        f"image fit ran on the shared database pool thread(s): {fit_threads}"
    )


@pytest.mark.asyncio
async def test_live_input_image_fit_does_not_run_on_the_shared_db_pool(
    monkeypatch, oversized_upload,
):
    """Input rendering must release its DB worker before image normalization."""
    from unittest.mock import AsyncMock

    from core import image_resize
    from infra import input_render
    from infra.thread_inputs import ClaimedBatch, ClaimedInput

    attachment, _path, _source = oversized_upload
    fit_threads: list[str] = []
    real_resize = image_resize.resize_image_for_provider

    def spying_resize(*args, **kwargs):
        fit_threads.append(threading.current_thread().name)
        return real_resize(*args, **kwargs)

    monkeypatch.setattr(image_resize, "resize_image_for_provider", spying_resize)
    monkeypatch.setattr(
        "infra.preflight_attempt.live_input_fits", AsyncMock(return_value=True),
    )
    batch = ClaimedBatch(
        _THREAD_ID, "turn", 1, 0, "selected", (ClaimedInput(
            "input", 1, 0, "user", "inspect", "apply_now", {},
            {"attachments": [attachment]}, "t0",
        ),),
    )

    result = await input_render.prepare_live_input(
        batch, object(), {"user_id": _OWNER_ID}, _target("claude-code", "unused"),
    )

    assert result is not None
    assert fit_threads, "expected the fit to actually run"
    assert not any(name.startswith("db") for name in fit_threads), (
        f"image fit ran on the shared database pool thread(s): {fit_threads}"
    )


def _container_upload(isolated_db, monkeypatch, tmp_path, *, file_id, filename, mime_type, data):
    """Register one thread upload in an arbitrary image container."""
    from infra import config
    from registry import models
    from shared_send_fixtures import _insert_session

    _insert_session(_THREAD_ID, user_id=_OWNER_ID, spawned_by=None)
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    path = tmp_path / ".threads" / _THREAD_ID / "uploads" / file_id / filename
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    models.register_artifact(
        integration="upload",
        type="upload",
        external_id=file_id,
        parent_thread_id=_THREAD_ID,
        path=str(path),
    )
    return {
        "file_id": file_id,
        "filename": filename,
        "mime_type": mime_type,
        "size": len(data),
        "url": f"/api/uploads/{_OWNER_ID}/{file_id}/{filename}",
    }


def _encoded(image_format: str, *, mode: str = "RGB", size=(64, 48)) -> bytes:
    image = Image.new(mode, size, (200, 60, 40))
    stream = BytesIO()
    image.save(stream, format=image_format)
    return stream.getvalue()


def _heic_bytes() -> bytes | None:
    """Return real HEIC bytes, or None where the optional opener is absent."""
    import pytest

    try:
        import pillow_heif
    except ImportError:
        pytest.skip("pillow-heif is not installed in this environment")
    pillow_heif.register_heif_opener()
    stream = BytesIO()
    Image.new("RGB", (64, 48), (200, 60, 40)).save(stream, format="HEIF")
    return stream.getvalue()


@pytest.mark.parametrize(
    ("harness", "provider"),
    [("native", "omlx"), ("claude-code", "unused"), ("codex", "unused")],
)
@pytest.mark.parametrize(
    ("image_format", "mime_type", "extension"),
    [
        ("TIFF", "image/tiff", "tif"),
        ("BMP", "image/bmp", "bmp"),
        ("PPM", "image/x-portable-pixmap", "ppm"),
    ],
)
def test_delivery_rewrites_a_container_the_target_cannot_open(
    isolated_db, monkeypatch, tmp_path, harness, provider, image_format, mime_type, extension
):
    """Every harness receives a documented container, including an unlimited target.

    ``omlx`` declares no size limits, which used to mean its bytes went out
    untouched; an unsupported container is a decode failure at the target, not a
    size question.
    """
    attachment = _container_upload(
        isolated_db,
        monkeypatch,
        tmp_path,
        file_id=f"{image_format}-{harness}",
        filename=f"photo.{extension}",
        mime_type=mime_type,
        data=_encoded(image_format),
    )

    _public, projection = _deliver([attachment], _target(harness, provider))

    image = projection.images[0]
    assert image.mime_type in {"image/jpeg", "image/png", "image/gif", "image/webp"}
    assert image.data_url().startswith(f"data:{image.mime_type};base64,")
    with Image.open(BytesIO(image.data)) as delivered:
        assert delivered.format is not None


def test_a_photo_picker_heic_delivers_as_a_provider_safe_image(isolated_db, monkeypatch, tmp_path):
    """The Photos picker legitimately offers HEIC; delivery has to honor that."""
    source = _heic_bytes()
    attachment = _container_upload(
        isolated_db,
        monkeypatch,
        tmp_path,
        file_id="heic-photo",
        filename="IMG_0001.heic",
        mime_type="image/heic",
        data=source,
    )

    _public, projection = _deliver([attachment], _target("native", "omlx"))

    image = projection.images[0]
    assert image.mime_type == "image/jpeg"
    with Image.open(BytesIO(image.data)) as delivered:
        assert delivered.format == "JPEG"


def test_delivery_keeps_the_admitted_source_digest_of_a_converted_image(
    isolated_db, monkeypatch, tmp_path
):
    """Conversion changes what the target receives, never what was admitted."""
    from hashlib import sha256 as _sha256

    source = _encoded("TIFF")
    attachment = _container_upload(
        isolated_db,
        monkeypatch,
        tmp_path,
        file_id="digest-tiff",
        filename="photo.tif",
        mime_type="image/tiff",
        data=source,
    )

    _public, projection = _deliver([attachment], _target("native", "omlx"))

    assert projection.digests == (_sha256(source).hexdigest(),)
    assert projection.images[0].sha256 != _sha256(source).hexdigest()


def _tiff_scan16() -> bytes:
    """Return a real 16-bit grayscale TIFF -- valid, admitted, unencodable as JPEG."""
    stream = BytesIO()
    Image.new("I;16", (64, 48)).save(stream, format="TIFF")
    return stream.getvalue()


def test_a_sixteen_bit_scan_delivers_as_a_provider_safe_image(
    isolated_db, monkeypatch, tmp_path
):
    """A 16-bit scan is admitted as a real TIFF and delivered as a real JPEG.

    ``I;16`` is the mode a flatbed scan or a scientific export arrives in, and
    JPEG has no such mode, so this pins the one admitted case where the pixel
    mode has to change before the bytes can leave.
    """
    source = _tiff_scan16()
    attachment = _container_upload(
        isolated_db,
        monkeypatch,
        tmp_path,
        file_id="scan16",
        filename="scan.tif",
        mime_type="image/tiff",
        data=source,
    )

    _public, projection = _deliver([attachment], _target("native", "omlx"))

    image = projection.images[0]
    assert image.mime_type in {"image/jpeg", "image/png"}
    with Image.open(BytesIO(image.data)) as delivered:
        assert delivered.mode == "RGB"


def test_an_unconvertible_image_declines_instead_of_raising(
    isolated_db, monkeypatch, tmp_path
):
    """Whatever conversion cannot handle becomes a structured decline.

    The conversion owner raises a plain ``ValueError``; delivery owns the
    translation into ``MediaProjectionError``. Without it the raw error escapes
    the decline contract and aborts the Turn building the request.
    """
    from core.media_projection import MediaProjectionError

    attachment = _container_upload(
        isolated_db,
        monkeypatch,
        tmp_path,
        file_id="broken",
        filename="broken.tif",
        mime_type="image/tiff",
        data=_encoded("TIFF"),
    )

    def _unconvertible(_data: bytes, _mime: str) -> tuple[bytes, str]:
        raise ValueError("the bytes cannot be converted")

    monkeypatch.setattr(
        "core.image_resize.normalize_image_for_provider", _unconvertible
    )

    with pytest.raises(MediaProjectionError) as excinfo:
        _deliver([attachment], _target("native", "omlx"))

    assert excinfo.value.cause == "image_corrupt"
    assert excinfo.value.legal_action == "upload_a_valid_image"


def _declares_pixels(width: int, height: int) -> bytes:
    """A TIFF that declares a huge frame and stores it in a few kilobytes."""
    stream = BytesIO()
    Image.new("L", (width, height), color=0).save(
        stream, format="TIFF", compression="tiff_lzw"
    )
    return stream.getvalue()


def _budget_of(monkeypatch, pixels: int) -> None:
    """Pin the host-derived decode budget so a test asserts one exact machine."""
    from core import image_resize

    monkeypatch.setattr(
        image_resize, "image_decode_pixel_budget", lambda _slots: pixels
    )


def test_an_oversized_frame_declines_in_the_terms_the_caller_supplied(monkeypatch):
    """The pre-decode bound is a size decline, and a reader never hears "upload"."""
    from core.media_fit import fit_image_bytes
    from core.media_projection import MediaProjectionError

    _budget_of(monkeypatch, 64_000_000)
    with pytest.raises(MediaProjectionError) as excinfo:
        fit_image_bytes(
            _declares_pixels(9000, 9000),
            "image/tiff",
            target=_target("native", "anthropic"),
            image_count=1,
            too_large_action="read_a_smaller_image",
            unreadable_action="read_the_file_again",
        )

    assert excinfo.value.cause == "image_too_large"
    assert excinfo.value.legal_action == "read_a_smaller_image"
    assert "upload" not in excinfo.value.detail.lower()


def test_an_oversized_frame_declines_an_upload_in_upload_terms(monkeypatch):
    """The same bound, reached from an upload, offers the upload's recovery."""
    from core.media_fit import fit_image_bytes
    from core.media_projection import MediaProjectionError

    _budget_of(monkeypatch, 64_000_000)
    with pytest.raises(MediaProjectionError) as excinfo:
        fit_image_bytes(
            _declares_pixels(9000, 9000),
            "image/tiff",
            target=_target("native", "anthropic"),
            image_count=1,
            too_large_action="upload_a_smaller_image",
            unreadable_action="upload_a_valid_image",
        )

    assert excinfo.value.cause == "image_too_large"
    assert excinfo.value.legal_action == "upload_a_smaller_image"


@pytest.mark.parametrize("mime_type", [None, 123, b"image/png", {"image/png": 1}])
def test_a_non_string_mime_classifies_as_a_file_and_never_raises(mime_type):
    """A descriptor whose MIME is not a string is an upload, not an exception."""
    from core.media_projection import _partition_attachments

    images, uploads = _partition_attachments(
        [{"mime_type": mime_type, "file_id": "f1", "filename": "scan.tiff"}]
    )

    assert images == []
    assert [item["file_id"] for item in uploads] == ["f1"]
