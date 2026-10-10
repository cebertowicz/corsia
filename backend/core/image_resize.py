"""Image format and size adaptation for provider requests.

Delivery (core.media_delivery) and native request building
(core.context_builder) call it before base64-encoding images for LLM API calls.

Two distinct adaptations, kept in the order every caller needs:
  ``normalize_image_for_provider`` rewrites a container into one every vision
  target can open (HEIC, TIFF, BMP, … -> JPEG or PNG);
  ``resize_image_for_provider`` then fits those bytes to one provider's limits.

Providers are documented for JPEG, PNG, GIF, and WebP -- the set named once in
``infra.config.VISION_MIME_TYPES``; anything else CORSIA accepts is converted
here, so no caller has to remember which containers exist.

Both reach their pixels through one internal decode, so the budget in
``_decoded_pixels`` applies whichever one a caller arrives through, and every
failure leaves as one ``ImageAdaptationError``.
"""

from __future__ import annotations

import io
import logging
import math
import threading

try:
    from PIL import Image, ImageOps
except ImportError:
    raise RuntimeError(
        "Pillow is required for image resize. Install with: pip install Pillow>=10.0.0"
    )

from infra.config import MAX_CONCURRENT_IMAGE_NORMALIZATIONS, VISION_MIME_TYPES
from infra.host_resources import image_decode_pixel_budget

logger = logging.getLogger(__name__)

# Decoding and re-encoding holds the full pixel buffer plus encoded copies in
# memory, so only this many run at once. Images already within their limits
# never take a slot. Callers must run off the event loop: acquiring blocks.
_NORMALIZATION_SLOTS = threading.Semaphore(MAX_CONCURRENT_IMAGE_NORMALIZATIONS)

# EXIF tag 274. Values 2-8 ask for a flip or a rotation; 1, a missing tag, and
# anything out of range all mean the stored pixels are already upright.
_ORIENTATION_TAG = 0x0112
_REORIENTING = frozenset({2, 3, 4, 5, 6, 7, 8})

_heif_state: str | None = None


class ImageAdaptationError(ValueError):
    """The one way adapting an image for a provider fails.

    ``cause`` names the dead end -- ``image_too_large`` for a frame beyond what
    a decode may cost, ``image_corrupt`` for bytes that will not read or write
    -- in the same vocabulary ``core.media_projection`` declines with. It never
    names a recovery: only the caller knows whether the image was uploaded or
    read, and telling a user to re-upload a file an agent opened is a dead end.

    A ``ValueError`` subclass because every caller already treats a failure here
    as one.
    """

    def __init__(self, message: str, cause: str) -> None:
        super().__init__(message)
        self.cause = cause


def heif_supported() -> bool:
    """Register Pillow's optional HEIF/HEIC opener once, and report availability.

    An Apple device hands the browser a HEIC photo from the ordinary photo
    picker, so this is the common image a user owns that plain Pillow cannot
    decode on its own. The opener ships as a separate wheel; when it is absent
    the rest of CORSIA keeps working and reports the missing capability by
    name instead of refusing every photo.
    """
    global _heif_state
    if _heif_state is None:
        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
        except ImportError:
            _heif_state = "missing"
            logger.info("pillow-heif is not installed; HEIC/HEIF input unavailable")
        else:
            _heif_state = "ready"
    return _heif_state == "ready"


def _carries_alpha(img: Image.Image) -> bool:
    """Return whether dropping alpha would visibly change this image."""
    return img.mode in {"RGBA", "LA"} or "transparency" in img.info


def _unreadable(mime_type: str) -> ImageAdaptationError:
    """Decline bytes that will not decode, or a mode no encoder will write."""
    return ImageAdaptationError(
        f"The {mime_type or 'uploaded'} image is corrupt or unreadable, so it "
        "could not be converted into a format this model can read.",
        "image_corrupt",
    )


def _open_header(data: bytes, mime_type: str) -> Image.Image:
    """Open one image header, which parses metadata and allocates no pixels.

    Pillow's own decompression-bomb ceiling fires here. It is a fixed guard
    against an implausible header rather than a statement about this machine, so
    it is reported as its own refusal and not as a damaged file.
    """
    try:
        return Image.open(io.BytesIO(data))
    except Image.DecompressionBombError as exc:
        raise ImageAdaptationError(
            "This image declares an implausible number of pixels and was "
            "refused as a possible decompression attack.",
            "image_too_large",
        ) from exc
    except Exception as exc:
        raise _unreadable(mime_type) from exc


def _upright(img: Image.Image) -> Image.Image:
    """Apply EXIF orientation, and only when the tag actually asks for one.

    ``ImageOps.exif_transpose`` returns a full-frame copy even for an image that
    is already upright, which is the common case and the largest avoidable
    allocation on this path. Reading the tag first costs nothing and keeps that
    copy for the images that need it. Damaged metadata is no reason to reject
    pixels that decoded, so a failure here keeps the stored orientation.
    """
    try:
        if img.getexif().get(_ORIENTATION_TAG) in _REORIENTING:
            return ImageOps.exif_transpose(img) or img
    except Exception as exc:
        logger.warning("could not apply EXIF orientation; using source pixels: %s", exc)
    return img


def _decoded_pixels(img: Image.Image, mime_type: str) -> Image.Image:
    """Materialize one opened header into upright pixels, within budget.

    The only decode in this module, so the budget it applies cannot be skipped
    by arriving through the other entry point. Cost tracks the pixels the header
    declares rather than the bytes on disk, and the declared frame is the only
    thing knowable before allocating: neither the HEIF nor the TIFF decoder
    supports ``Image.draft``, so a frame above the budget is declined outright
    rather than decoded smaller. What the budget is comes from this host's RAM
    (``infra.host_resources``), because the largest photograph a machine can
    safely open is a property of that machine. It is asked for here rather than
    held as a constant so that importing this module never reads the host.
    """
    budget = image_decode_pixel_budget(MAX_CONCURRENT_IMAGE_NORMALIZATIONS)
    width, height = img.size
    if width * height > budget:
        img.close()
        raise ImageAdaptationError(
            f"This device does not have enough memory to open an image this "
            f"large: it is {width}x{height} pixels "
            f"({width * height / 1_000_000:.0f} megapixels), and this machine "
            f"can open up to {budget / 1_000_000:.0f} megapixels.",
            "image_too_large",
        )
    try:
        img.load()
    except Exception as exc:
        raise _unreadable(mime_type) from exc
    return _upright(img)


def normalize_image_for_provider(data: bytes, mime_type: str) -> tuple[bytes, str]:
    """Rewrite one image into a container every vision provider can open.

    Bytes already in a documented container come back untouched so the caller
    skips the decode. A provider's own size limits are a separate concern and
    are applied to the result by ``resize_image_for_provider``.

    Raises:
        ImageAdaptationError: If the declared frame exceeds the decode budget,
            or the bytes cannot be decoded or re-encoded. Header verification
            passing does not mean the full pixel data decodes, and an encoder
            can still refuse the mode it is handed.
    """
    if mime_type in VISION_MIME_TYPES:
        return data, mime_type
    heif_supported()
    opened = _open_header(data, mime_type)
    with _NORMALIZATION_SLOTS:
        img = _decoded_pixels(opened, mime_type)
        try:
            if _carries_alpha(img):
                if img.mode not in {"RGBA", "LA", "P"}:
                    img = img.convert("RGBA")
                return _encode_image(img, "image/png")
            return _encode_image(img, "image/jpeg")
        except Exception as exc:
            raise _unreadable(mime_type) from exc


def _active_max_dimension(limits: dict, image_count: int) -> int | None:
    """Return the provider dimension tier for the complete request."""
    maximum = limits.get("max_dimension")
    threshold = limits.get("multi_image_threshold")
    many_maximum = limits.get("max_dimension_multi")
    if maximum is None:
        return None
    if threshold and image_count > threshold and many_maximum is not None:
        return many_maximum
    return maximum


def _encode_within_byte_limit(
    image: Image.Image,
    mime_type: str,
    max_bytes: int,
) -> tuple[bytes, str] | None:
    """Encode one image at supported quality tiers, returning the first fitting result."""
    for quality in (95, 85, 75, 60, 40):
        result_bytes, output_mime = _encode_image(image, mime_type, quality=quality)
        if len(result_bytes) <= max_bytes:
            return result_bytes, output_mime
    return None


def _downscale_for_byte_limit(
    image: Image.Image,
    mime_type: str,
    max_bytes: int,
) -> tuple[bytes, str] | None:
    """Downscale-only fit for an image whose quality tiers remain too large."""
    current = image
    for _attempt in range(8):
        result = _encode_within_byte_limit(current, mime_type, max_bytes)
        if result is not None:
            return result

        smallest_bytes, _output_mime = _encode_image(current, mime_type, quality=40)
        scale = min(0.9, math.sqrt(max_bytes / len(smallest_bytes)) * 0.95)
        width, height = current.size
        next_size = (
            max(1, round(width * scale)),
            max(1, round(height * scale)),
        )
        if next_size == current.size:
            return None
        current = current.resize(next_size, Image.Resampling.LANCZOS)
    return _encode_within_byte_limit(current, mime_type, max_bytes)


def resize_image_for_provider(
    data: bytes,
    mime_type: str,
    limits: dict | None,
    image_count: int = 1,
) -> tuple[bytes, str]:
    """Resize/compress image bytes to fit within provider limits.

    Args:
        data: Raw image bytes from disk or upload.
        mime_type: MIME type of the input (e.g. "image/png").
        limits: Provider-specific limits dict from PROVIDER_IMAGE_LIMITS, or None.
            If None, returns data unchanged.
        image_count: Total images in this request (for Anthropic's multi-image
            dimension threshold).

    Returns:
        (bytes, mime_type) — mime may change if PNG→JPEG compression needed.

    Raises:
        ImageAdaptationError: If the declared frame exceeds the decode budget,
            the image cannot be brought under limits, or the bytes are corrupt.
            The message is user-facing; the recovery is the caller's to name.
    """
    if limits is None:
        return data, mime_type

    # Pillow's process-global decompression-bomb limit remains enabled. Delivery
    # runs concurrently, so temporarily disabling it can leak that unsafe state
    # into unrelated image reads or leave it disabled after interleaved restores.
    img = _open_header(data, mime_type)

    # Opening reads only the header, and EXIF rotation never changes the longest
    # side, so an image within its limits returns unchanged without decoding --
    # and the decode budget never refuses bytes that are delivered as they are.
    active_max_dim = _active_max_dimension(limits, image_count)
    needs_resize = active_max_dim is not None and max(img.size) > active_max_dim
    max_bytes = limits.get("max_bytes")
    if max_bytes is not None and len(data) <= max_bytes and not needs_resize:
        return data, mime_type

    with _NORMALIZATION_SLOTS:
        return _fit_decoded_image(
            _decoded_pixels(img, mime_type),
            len(data),
            mime_type,
            active_max_dim,
            max_bytes,
        )


def _fit_decoded_image(
    img: Image.Image,
    source_bytes: int,
    mime_type: str,
    active_max_dim: int | None,
    max_bytes: int | None,
) -> tuple[bytes, str]:
    """Resize and re-encode one decoded image that exceeds its provider limits."""
    if active_max_dim is not None and max(img.size) > active_max_dim:
        width, height = img.size
        scale = active_max_dim / max(width, height)
        img = img.resize(
            (max(1, round(width * scale)), max(1, round(height * scale))),
            Image.Resampling.LANCZOS,
        )

    if max_bytes is None:
        return _encode_image(img, mime_type)

    fitted = _downscale_for_byte_limit(img, mime_type, max_bytes)
    if fitted is not None:
        return fitted

    size_mb = source_bytes / (1024 * 1024)
    limit_mb = max_bytes / (1024 * 1024)
    raise ImageAdaptationError(
        f"Image is {size_mb:.1f}MB and cannot be fitted under the "
        f"{limit_mb:.0f}MB limit. Choose a smaller image.",
        "image_too_large",
    )


def _encode_image(
    img: Image.Image, mime_type: str, quality: int | None = None
) -> tuple[bytes, str]:
    """Encode a PIL Image to (bytes, mime_type) pair.

    PNG stays PNG (lossless) unless quality < 95 is specified, in which case
    it converts to JPEG for compression.
    """
    buf = io.BytesIO()

    if mime_type == "image/png":
        if quality is not None and quality < 95:
            if img.mode == "RGBA":
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[3])
                img = bg
            else:
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            return buf.getvalue(), "image/jpeg"

        save_kwargs: dict = {}
        if img.mode == "RGBA":
            save_kwargs["optimize"] = True

        img.save(buf, format="PNG", **save_kwargs)
        return buf.getvalue(), "image/png"

    elif mime_type == "image/webp":
        q = quality if quality is not None else 90
        img.save(buf, format="WEBP", quality=q)
        return buf.getvalue(), "image/webp"

    else:
        q = quality if quality is not None else 90
        if img.mode == "RGBA":
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=q)
        return buf.getvalue(), "image/jpeg"
