"""Content verification for admitted image uploads.

Proves one resolved upload's bytes are the admitted source: within the byte
limit, unchanged since admission, and decodable as an image; and that no
non-image upload is secretly an image. The declared MIME is advisory -- the bytes
decide. It never resizes or re-encodes; fitting source bytes to a target's
provider limits is delivery work owned by ``core.media_delivery``.
Reference/ownership resolution stays in ``core.media_projection``.
"""

from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO, Sequence

from PIL import Image

from core.media_projection import (
    MediaProjectionError,
    ValidatedImage,
    _reject,
    _resolve_upload,
)
from core.media_sniffing import IMAGE_SNIFF_BYTES, image_mime_from_prefix

# Truthful source MIME for the containers a real camera, phone, or screenshot
# tool produces. Decoding is the acceptance test, not this map: an entry only
# names the format for logs and declines, and delivery (core.media_delivery)
# converts anything outside infra.config.VISION_MIME_TYPES before it ships.
_SOURCE_MIME_BY_FORMAT = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "MPO": "image/jpeg",
    "GIF": "image/gif",
    "WEBP": "image/webp",
    "BMP": "image/bmp",
    "DIB": "image/bmp",
    "TIFF": "image/tiff",
    "HEIF": "image/heif",
    "JPEG2000": "image/jp2",
    "PPM": "image/x-portable-pixmap",
    "PGM": "image/x-portable-graymap",
    "PBM": "image/x-portable-bitmap",
    "ICO": "image/vnd.microsoft.icon",
    "SGI": "image/sgi",
    "TGA": "image/x-tga",
    "XBM": "image/x-xbitmap",
}

_HEIF_MIME_TYPES = {"image/heic", "image/heif", "image/heic-sequence", "image/heif-sequence"}

# Containers a picker will happily hand over that no raster decoder can read.
# Naming them keeps the decline actionable instead of calling a valid file corrupt.
_UNDECODABLE_IMAGE_MIMES = {
    "image/svg+xml": "Vector (SVG) images are not supported.",
}


def _looks_like_heif(declared_mime: Any, data: bytes) -> bool:
    """Return whether this upload is a HEIF/HEIC, by declared type or by brand."""
    if isinstance(declared_mime, str) and declared_mime.lower() in _HEIF_MIME_TYPES:
        return True
    return image_mime_from_prefix(data) in _HEIF_MIME_TYPES


def _undecodable_decline(declared_mime: Any) -> MediaProjectionError:
    """Decline bytes no decoder can read, naming the container when it is known."""
    if isinstance(declared_mime, str) and declared_mime in _UNDECODABLE_IMAGE_MIMES:
        return _reject(
            "image_corrupt",
            _UNDECODABLE_IMAGE_MIMES[declared_mime],
            "upload_a_png_jpeg_gif_or_webp_image",
        )
    return _reject(
        "image_corrupt",
        "An image is corrupt or unreadable.",
        "upload_a_valid_image",
    )


def _verified_image_mime(source: BinaryIO) -> str:
    """Return the canonical MIME after Pillow verifies one seekable stream."""
    with Image.open(source) as image:
        detected_format = str(image.format or "").upper()
        image.verify()
    if not detected_format:
        raise ValueError("image decoder returned no format")
    return _SOURCE_MIME_BY_FORMAT.get(detected_format) or (
        f"image/{detected_format.lower()}"
    )


def image_mime_from_stream(source: BinaryIO) -> str | None:
    """Return image identity for any Pillow-decodable stream, preserving position."""
    from core.image_resize import heif_supported

    position = source.tell()
    heif_supported()
    source.seek(0)
    try:
        return _verified_image_mime(source)
    except (Image.DecompressionBombError, OSError, SyntaxError, ValueError):
        return None
    finally:
        source.seek(position)


def detect_and_verify_image(data: bytes, declared_mime: Any) -> str:
    """Prove bytes decode as an image, and return the MIME those bytes really are.

    The declared MIME never decides acceptance: uploads and tool media reads both
    land here, and any container Pillow can decode is admitted. Rewriting that
    image into a container the target can open is delivery's job
    (``core.media_delivery``), so a HEIC photo or a TIFF screenshot reaches the
    user converted rather than rejected. Only undecodable bytes are refused, and
    the refusal names what was found.
    """
    from core.image_resize import heif_supported

    opener_available = heif_supported()
    try:
        return _verified_image_mime(BytesIO(data))
    except Exception as exc:
        if not opener_available and _looks_like_heif(declared_mime, data):
            raise _reject(
                "image_heif_unavailable",
                "HEIC and HEIF images need the optional HEIF support package.",
                "install_the_heif_package_or_upload_jpeg_or_png",
            ) from exc
        raise _undecodable_decline(declared_mime) from exc


def read_validated_image(
    attachment: dict,
    *,
    owner_id: str,
    thread_id: str,
    thread_upload_root: Path,
    max_image_bytes: int,
    expected_digest: str | None,
) -> ValidatedImage:
    """Read one admitted upload and prove it is the unchanged source image."""
    attachment_id, path = _resolve_upload(
        attachment,
        owner_id=owner_id,
        thread_id=thread_id,
        thread_upload_root=thread_upload_root,
    )
    try:
        with path.open("rb") as stream:
            data = stream.read(max_image_bytes + 1)
    except OSError:
        raise _reject(
            "image_not_found",
            "The authenticated image upload is no longer available.",
            "upload_the_image_again",
        ) from None
    if len(data) > max_image_bytes:
        raise _reject(
            "image_too_large",
            "An image exceeds the per-image byte limit.",
            "upload_a_smaller_image",
        )
    digest = sha256(data).hexdigest()
    if expected_digest is not None and digest != expected_digest:
        raise _reject(
            "image_digest_changed",
            "An image changed after its delivery was admitted.",
            "upload_the_image_again",
        )
    declared_size = attachment.get("size")
    if declared_size is not None and (
        not isinstance(declared_size, int) or declared_size != len(data)
    ):
        raise _reject(
            "image_size_changed",
            "An image no longer matches its authenticated upload size.",
            "upload_the_image_again",
        )
    detected_mime = detect_and_verify_image(data, attachment.get("mime_type"))
    return ValidatedImage(
        attachment_id=attachment_id,
        mime_type=detected_mime,
        data=data,
        sha256=digest,
        byte_size=len(data),
    )


def reject_misdeclared_images(
    attachments: Sequence[dict],
    *,
    owner_id: str,
    thread_id: str,
    thread_upload_root: Path,
) -> None:
    """Reject a non-image upload whose bytes are actually an image."""
    image_suffixes = (".png", ".jpg", ".jpeg", ".gif", ".webp")
    for attachment in attachments:
        try:
            _attachment_id, path = _resolve_upload(
                attachment,
                owner_id=owner_id,
                thread_id=thread_id,
                thread_upload_root=thread_upload_root,
            )
        except MediaProjectionError as exc:
            mime_type = attachment.get("mime_type")
            filename = str(attachment.get("filename") or "").lower()
            clearly_non_image = (
                isinstance(mime_type, str)
                and not mime_type.startswith("image/")
                and not filename.endswith(image_suffixes)
            )
            if exc.cause == "image_not_found" and clearly_non_image:
                continue
            raise
        try:
            with path.open("rb") as stream:
                prefix = stream.read(IMAGE_SNIFF_BYTES)
        except OSError:
            raise _reject(
                "image_not_found",
                "The authenticated upload is no longer available.",
                "upload_the_file_again",
            ) from None
        if image_mime_from_prefix(prefix) is not None:
            raise _reject(
                "image_mime_mismatch",
                "An image's bytes do not match its declared format.",
                "remove_and_upload_the_image_again",
            )
