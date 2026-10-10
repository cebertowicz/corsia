
from __future__ import annotations

import io
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from PIL import Image  # noqa: E402
from core.image_resize import resize_image_for_provider  # noqa: E402


def _make_png(width, height=None, color="red"):
    if isinstance(width, tuple):
        width, height = width
    img = Image.new("RGB", (width, height), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_jpeg(width, height=None, color="blue", quality=95):
    if isinstance(width, tuple):
        width, height = width
    img = Image.new("RGB", (width, height), color=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _jpeg_declaring_orientation(orientation: int, size=(100, 100)) -> bytes:
    """A JPEG whose EXIF asks to be reoriented, so a transpose is attempted."""
    image = Image.new("RGB", size, color="blue")
    exif = image.getexif()
    exif[0x0112] = orientation
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=95, exif=exif)
    return buf.getvalue()


def _make_noise_jpeg(width: int, height: int) -> bytes:
    """Return a real high-entropy JPEG that cannot fit by quality alone."""
    image = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=100, subsampling=0)
    return buf.getvalue()


ANTHROPIC_LIMITS = {
    "max_bytes": 5 * 1024 * 1024,
    "max_dimension": 8000,
}

OPENAI_LIMITS = {
    "max_bytes": 20 * 1024 * 1024,
    "max_dimension": None,
}

NO_LIMITS = None


class TestNoLimits:

    def test_concurrent_resize_preserves_global_pixel_limit(self, monkeypatch):
        """Parallel delivery cannot leave Pillow's process safety limit disabled."""
        from core import image_resize

        original = Image.MAX_IMAGE_PIXELS
        entered = threading.Barrier(2)
        first_returned = threading.Event()
        assignment_lock = threading.Lock()
        assignment = {}
        next_index = iter((0, 1))

        class FakeImage:
            format = "PNG"
            mode = "RGB"
            size = (1, 1)

            def load(self):
                return None

            def getexif(self):
                return {}

        def fake_open(_stream):
            with assignment_lock:
                index = next(next_index)
                assignment[threading.get_ident()] = index
            entered.wait(timeout=5)
            if index == 1:
                assert first_returned.wait(timeout=5)
            return FakeImage()

        def invoke():
            result = image_resize.resize_image_for_provider(
                b"image",
                "image/png",
                {"max_bytes": 1, "max_dimension": 1},
            )
            if assignment[threading.get_ident()] == 0:
                first_returned.set()
            return result

        monkeypatch.setattr(image_resize.Image, "open", fake_open)
        monkeypatch.setattr(
            image_resize,
            "_encode_image",
            lambda _image, mime_type, quality=None: (b"x", mime_type),
        )
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(invoke) for _index in range(2)]
                assert [future.result(timeout=10) for future in futures]
            assert Image.MAX_IMAGE_PIXELS == original
        finally:
            Image.MAX_IMAGE_PIXELS = original

    def test_concurrent_resize_respects_normalization_bound(self, monkeypatch):
        """More callers than MAX_CONCURRENT_IMAGE_NORMALIZATIONS queue rather than run at once."""
        from core import image_resize
        from infra.config import MAX_CONCURRENT_IMAGE_NORMALIZATIONS

        lock = threading.Lock()
        current = 0
        peak = 0
        release = threading.Event()

        def fake_decode():
            nonlocal current, peak
            with lock:
                current += 1
                peak = max(peak, current)
            release.wait(timeout=5)
            with lock:
                current -= 1

        class FakeImage:
            format = "PNG"
            mode = "RGB"
            size = (1, 1)

            def load(self):
                fake_decode()

            def getexif(self):
                return {}

        monkeypatch.setattr(image_resize.Image, "open", lambda _stream: FakeImage())
        monkeypatch.setattr(
            image_resize,
            "_encode_image",
            lambda _image, mime_type, quality=None: (b"x", mime_type),
        )

        def invoke():
            return image_resize.resize_image_for_provider(
                b"image", "image/png", {"max_bytes": 1, "max_dimension": 1},
            )

        worker_count = MAX_CONCURRENT_IMAGE_NORMALIZATIONS + 2
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(invoke) for _ in range(worker_count)]
            # Let every thread contend for a slot before releasing any of them.
            threading.Event().wait(0.3)
            assert peak == MAX_CONCURRENT_IMAGE_NORMALIZATIONS
            release.set()
            assert [future.result(timeout=5) for future in futures]

    def test_image_within_limits_never_waits_for_a_normalization_slot(self):
        """An image that already fits returns unchanged even when every slot is busy."""
        from core import image_resize
        from infra.config import MAX_CONCURRENT_IMAGE_NORMALIZATIONS

        data = _make_png(100, 100)
        held = 0
        try:
            for _slot in range(MAX_CONCURRENT_IMAGE_NORMALIZATIONS):
                assert image_resize._NORMALIZATION_SLOTS.acquire(timeout=5)
                held += 1
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    image_resize.resize_image_for_provider,
                    data,
                    "image/png",
                    ANTHROPIC_LIMITS,
                )
                assert future.result(timeout=5) == (data, "image/png")
        finally:
            for _slot in range(held):
                image_resize._NORMALIZATION_SLOTS.release()

    def test_png_passthrough(self):
        data = _make_png(100, 100)
        result, mime = resize_image_for_provider(data, "image/png", NO_LIMITS)
        assert result == data
        assert mime == "image/png"

    def test_jpeg_passthrough(self):
        data = _make_jpeg(100, 100)
        result, mime = resize_image_for_provider(data, "image/jpeg", NO_LIMITS)
        assert result == data
        assert mime == "image/jpeg"


class TestAnthropicDimensionLimit:

    def test_small_image_unchanged(self):
        data = _make_png(100, 100)
        result, mime = resize_image_for_provider(data, "image/png", ANTHROPIC_LIMITS)
        assert result == data

    def test_resize_preserves_aspect_ratio(self):
        data = _make_png((8005, 4803))
        result, mime = resize_image_for_provider(data, "image/png", ANTHROPIC_LIMITS)
        img = Image.open(io.BytesIO(result))
        assert max(img.size) == 8000
        assert img.size[0] / img.size[1] == 5.0 / 3.0

    def test_resize_tall_image(self):
        data = _make_png((4803, 8005))
        result, mime = resize_image_for_provider(data, "image/png", ANTHROPIC_LIMITS)
        img = Image.open(io.BytesIO(result))
        assert max(img.size) == 8000

    def test_very_large_image_resized(self):
        data = _make_png((8001, 8))
        result, mime = resize_image_for_provider(data, "image/png", ANTHROPIC_LIMITS)
        img = Image.open(io.BytesIO(result))
        assert max(img.size) == 8000


class TestAnthropicFileSizeLimit:

    def test_png_under_limit_unchanged(self):
        data = _make_png(100, 100)
        result, mime = resize_image_for_provider(data, "image/png", ANTHROPIC_LIMITS)
        assert result == data

    def test_jpeg_compressed_when_over_limit(self):
        data = _make_jpeg((100, 100), quality=95) + b"\0" * (5 * 1024 * 1024)
        assert len(data) > 5 * 1024 * 1024, f"Test image {len(data)} bytes should be >5MB"

        result, mime = resize_image_for_provider(
            data, "image/jpeg", ANTHROPIC_LIMITS
        )

        assert len(result) <= 5 * 1024 * 1024
        assert mime == "image/jpeg"

    def test_jpeg_downscales_when_quality_cannot_meet_byte_limit(self):
        """Fitting a large upload keeps reducing pixels instead of rejecting it."""
        data = _make_noise_jpeg(1000, 1000)
        limits = {"max_bytes": 50 * 1024, "max_dimension": None}

        result, mime = resize_image_for_provider(data, "image/jpeg", limits)

        assert len(result) <= limits["max_bytes"]
        assert mime == "image/jpeg"
        with Image.open(io.BytesIO(result)) as image:
            assert max(image.size) < 1000


class TestOpenaiLimits:

    def test_large_jpeg_passes_through(self):
        data = _make_jpeg((8001, 8), quality=95)
        result, mime = resize_image_for_provider(data, "image/jpeg", OPENAI_LIMITS)
        assert result == data


class TestMultiImageThreshold:

    def test_single_image_uses_standard_limit(self):
        data = _make_png((8001, 8))
        result, mime = resize_image_for_provider(
            data, "image/png", ANTHROPIC_LIMITS
        )
        img = Image.open(io.BytesIO(result))
        assert max(img.size) == 8000

    def test_multi_image_uses_tighter_limit(self):
        limits = {
            "max_bytes": 5 * 1024 * 1024,
            "max_dimension": 8000,
            "max_dimension_multi": 2000,
            "multi_image_threshold": 20,
        }
        data = _make_png((2001, 8))
        result, mime = resize_image_for_provider(
            data, "image/png", limits, image_count=21
        )
        img = Image.open(io.BytesIO(result))
        assert max(img.size) == 2000

    def test_at_threshold_uses_standard_limit(self):
        limits = {
            "max_bytes": 5 * 1024 * 1024,
            "max_dimension": 8000,
            "max_dimension_multi": 2000,
            "multi_image_threshold": 20,
        }
        data = _make_png((9000, 6000))
        result, mime = resize_image_for_provider(
            data, "image/png", limits, image_count=20
        )
        img = Image.open(io.BytesIO(result))
        assert max(img.size) == 8000


class TestErrorHandling:

    def test_damaged_exif_metadata_keeps_the_image_and_logs_a_warning(self, monkeypatch, caplog):
        """A metadata-only failure must not reject pixels that still need fitting."""
        from core import image_resize

        data = _jpeg_declaring_orientation(6)
        limits = {"max_bytes": 5 * 1024 * 1024, "max_dimension": 50}

        def broken_transpose(_image):
            raise OSError("damaged EXIF")

        monkeypatch.setattr(image_resize.ImageOps, "exif_transpose", broken_transpose)

        with caplog.at_level(logging.WARNING, logger="core.image_resize"):
            result, mime = resize_image_for_provider(data, "image/jpeg", limits)

        assert mime == "image/jpeg"
        with Image.open(io.BytesIO(result)) as fitted:
            assert max(fitted.size) == 50
        assert "could not apply EXIF orientation" in caplog.text

    def test_corrupt_data_raises_value_error(self):
        try:
            resize_image_for_provider(b"not an image", "image/png", ANTHROPIC_LIMITS)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "corrupt" in str(e).lower() or "unreadable" in str(e).lower()

    def test_unresizable_image_raises_value_error(self):
        data = _make_jpeg((6000, 6000), quality=95)
        tight_limits = {
            "max_bytes": 100,  # 100 bytes — impossible to fit
            "max_dimension": 8000,
        }
        try:
            resize_image_for_provider(data, "image/jpeg", tight_limits)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "MB" in str(e)

    def test_empty_data_raises_value_error(self):
        try:
            resize_image_for_provider(b"", "image/png", ANTHROPIC_LIMITS)
            assert False, "Should have raised ValueError"
        except ValueError:
            pass


class TestFormatHandling:

    def test_jpeg_input_stays_jpeg(self):
        data = _make_jpeg(100, 100)
        result, mime = resize_image_for_provider(data, "image/jpeg", ANTHROPIC_LIMITS)
        assert mime == "image/jpeg"

    def test_jpeg_resize_preserves_format(self):
        data = _make_jpeg((9000, 6000))
        result, mime = resize_image_for_provider(data, "image/jpeg", ANTHROPIC_LIMITS)
        assert mime == "image/jpeg"
        img = Image.open(io.BytesIO(result))
        assert max(img.size) == 8000

    def test_webp_input(self):
        img = Image.new("RGB", (100, 100), color="purple")
        buf = io.BytesIO()
        img.save(buf, format="WEBP", quality=90)
        webp_data = buf.getvalue()

        result, mime = resize_image_for_provider(
            webp_data, "image/webp", ANTHROPIC_LIMITS
        )
        assert mime == "image/webp"


def _make_in_format(fmt, mode="RGB"):
    img = Image.new(mode, (48, 32), (200, 60, 40))
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def test_provider_safe_containers_return_their_bytes_untouched():
    """A documented container costs one set-membership test, never a decode."""
    from core.image_resize import normalize_image_for_provider

    for mime, fmt in (
        ("image/png", "PNG"),
        ("image/jpeg", "JPEG"),
        ("image/gif", "GIF"),
        ("image/webp", "WEBP"),
    ):
        data = _make_in_format(fmt)
        assert normalize_image_for_provider(data, mime) == (data, mime)


def test_an_undocumented_container_is_rewritten_to_one_every_provider_opens():
    from core.image_resize import normalize_image_for_provider

    data, mime = normalize_image_for_provider(_make_in_format("TIFF"), "image/tiff")

    assert mime == "image/jpeg"
    with Image.open(io.BytesIO(data)) as delivered:
        assert delivered.format == "JPEG"


def test_transparency_survives_the_rewrite():
    """Flattening an alpha channel onto white would visibly change the image."""
    from core.image_resize import normalize_image_for_provider

    data, mime = normalize_image_for_provider(
        _make_in_format("TIFF", "RGBA"), "image/tiff"
    )

    assert mime == "image/png"
    with Image.open(io.BytesIO(data)) as delivered:
        assert delivered.mode == "RGBA"


def test_undecodable_bytes_raise_a_user_facing_error():
    from core.image_resize import normalize_image_for_provider

    with pytest.raises(ValueError, match="could not be converted"):
        normalize_image_for_provider(b"definitely-not-an-image", "image/tiff")


@pytest.mark.parametrize("mode", ["I;16", "I", "F", "P", "1", "CMYK", "L", "RGB"])
def test_every_admitted_mode_reaches_a_writable_container(mode):
    """A container is only half of a valid delivery; the pixel mode is the other.

    Admission accepts any container Pillow can decode, which includes modes the
    target encoder cannot write. This pins the guarantee that conversion covers
    every one of them, so nothing admitted can fail on the way out.
    """
    from core.image_resize import normalize_image_for_provider

    stream = io.BytesIO()
    Image.new(mode, (32, 24)).save(stream, format="TIFF")

    data, mime = normalize_image_for_provider(stream.getvalue(), "image/tiff")

    assert mime in {"image/jpeg", "image/png"}
    with Image.open(io.BytesIO(data)) as delivered:
        assert delivered.format in {"JPEG", "PNG"}


def test_an_unfitted_image_cannot_be_delivered_as_a_data_url():
    """Skipping core.media_delivery must fail loudly, not at the provider."""
    from core.media_projection import ValidatedImage

    unfitted = ValidatedImage(
        attachment_id="a",
        mime_type="image/tiff",
        data=b"x",
        sha256="0" * 64,
        byte_size=1,
    )

    with pytest.raises(ValueError, match="core.media_delivery"):
        unfitted.data_url()


def _declares_pixels(width: int, height: int) -> bytes:
    """A TIFF that declares a huge frame and stores it in a few kilobytes.

    The cost of a decode follows the pixels a header declares, not the bytes on
    disk. A uniform frame under LZW is the cheapest way to author that gap, so
    it is what a byte-size limit cannot see and a declared-size limit must.
    """
    stream = io.BytesIO()
    Image.new("L", (width, height), color=0).save(
        stream, format="TIFF", compression="tiff_lzw"
    )
    return stream.getvalue()


def _forbid_decoding(monkeypatch):
    """Fail loudly if anything materializes pixels after this point."""

    def _decode(_self):
        raise AssertionError("the budget must decline before any pixel is allocated")

    monkeypatch.setattr(Image.Image, "load", _decode)


def _budget_of(monkeypatch, pixels: int) -> None:
    """Pin the host-derived decode budget so a test asserts one exact machine."""
    from core import image_resize

    monkeypatch.setattr(
        image_resize, "image_decode_pixel_budget", lambda _slots: pixels
    )


def test_the_decode_budget_is_asked_for_and_never_frozen_at_import(monkeypatch):
    """Holding a copy would answer for whichever machine imported the module."""
    from core.image_resize import normalize_image_for_provider

    data = _declares_pixels(9000, 9000)
    _budget_of(monkeypatch, 64_000_000)
    with pytest.raises(ValueError):
        normalize_image_for_provider(data, "image/tiff")

    _budget_of(monkeypatch, 1_073_741_824)
    _bytes, mime = normalize_image_for_provider(data, "image/tiff")

    assert mime == "image/jpeg"


def test_a_frame_above_the_decode_budget_is_declined_before_any_decode(monkeypatch):
    """A declared frame beyond the budget must cost no pixel buffer at all."""
    from core.image_resize import normalize_image_for_provider

    data = _declares_pixels(9000, 9000)
    assert len(data) < 1024 * 1024, "the fixture must be small enough to slip a byte cap"
    _budget_of(monkeypatch, 64_000_000)
    _forbid_decoding(monkeypatch)

    with pytest.raises(ValueError) as excinfo:
        normalize_image_for_provider(data, "image/tiff")

    assert excinfo.value.cause == "image_too_large"
    assert "9000x9000" in str(excinfo.value)
    assert "64 megapixel" in str(excinfo.value)
    assert "does not have enough memory" in str(excinfo.value)
    assert "upload" not in str(excinfo.value).lower()


def test_the_decode_budget_covers_the_resize_entry_point(monkeypatch):
    """A bound one entry point applies and the other skips is not a bound."""
    from core.image_resize import resize_image_for_provider

    data = _declares_pixels(9000, 9000)
    _budget_of(monkeypatch, 64_000_000)
    _forbid_decoding(monkeypatch)

    with pytest.raises(ValueError) as excinfo:
        resize_image_for_provider(data, "image/tiff", ANTHROPIC_LIMITS)

    assert excinfo.value.cause == "image_too_large"


def test_the_decode_budget_never_refuses_bytes_that_are_passed_through(monkeypatch):
    """It bounds what a decode may cost, so it cannot refuse a delivery."""
    from core.image_resize import resize_image_for_provider

    data = _declares_pixels(9000, 9000)
    _budget_of(monkeypatch, 64_000_000)
    no_dimension_limit = {"max_bytes": 20 * 1024 * 1024, "max_dimension": None}

    assert resize_image_for_provider(data, "image/tiff", no_dimension_limit) == (
        data,
        "image/tiff",
    )


def test_the_decode_budget_is_the_one_this_host_derives():
    """One owner for the number, so the decline matches the startup log."""
    from infra.config import MAX_CONCURRENT_IMAGE_NORMALIZATIONS
    from infra.host_resources import (
        host_total_ram_bytes,
        image_decode_pixel_budget,
        max_image_decode_pixels,
    )

    assert image_decode_pixel_budget(
        MAX_CONCURRENT_IMAGE_NORMALIZATIONS
    ) == max_image_decode_pixels(
        host_total_ram_bytes(), MAX_CONCURRENT_IMAGE_NORMALIZATIONS
    )


def test_reorienting_an_upright_image_would_copy_the_whole_frame():
    """The premise of the check: ``exif_transpose`` copies with no tag to apply."""
    from PIL import ImageOps

    with Image.open(io.BytesIO(_make_jpeg(48, 32))) as plain:
        plain.load()
        assert ImageOps.exif_transpose(plain) is not plain


def test_an_image_declaring_no_orientation_keeps_its_own_frame():
    """The common case is upright, so it must not pay for that copy."""
    from core import image_resize

    with Image.open(io.BytesIO(_make_jpeg(48, 32))) as plain:
        plain.load()
        assert image_resize._upright(plain) is plain


def test_a_declared_orientation_still_reorients_the_frame():
    """Skipping the copy must not skip a rotation the image actually asks for."""
    from core import image_resize

    sideways = Image.open(io.BytesIO(_jpeg_declaring_orientation(6, (48, 32))))
    sideways.load()

    assert image_resize._upright(sideways).size == (32, 48)


def _tiff_with_orientation(orientation: int) -> bytes:
    """A TIFF whose header asks for one of the seven reorienting values."""
    stream = io.BytesIO()
    Image.new("RGB", (48, 32), (200, 60, 40)).save(
        stream, format="TIFF", tiffinfo={0x0112: orientation}
    )
    return stream.getvalue()


def test_a_rewritten_container_is_delivered_upright():
    """The composed path still hands the provider pixels the right way up."""
    from core.image_resize import normalize_image_for_provider

    data, _mime = normalize_image_for_provider(_tiff_with_orientation(6), "image/tiff")

    with Image.open(io.BytesIO(data)) as delivered:
        assert delivered.size == (32, 48)
