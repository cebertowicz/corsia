"""Signature-only media classification shared by admission paths."""

from __future__ import annotations

# ISO base media ``ftyp`` major brands of the HEIF family, by the MIME each
# container is registered under: still images, then image sequences.
_HEIF_BRAND_MIME = {
    b"heic": "image/heic",
    b"heix": "image/heic",
    b"heim": "image/heic",
    b"heis": "image/heic",
    b"hevc": "image/heic-sequence",
    b"hevx": "image/heic-sequence",
    b"hevm": "image/heic-sequence",
    b"hevs": "image/heic-sequence",
    b"mif1": "image/heif",
    b"msf1": "image/heif-sequence",
}

IMAGE_SNIFF_BYTES = 18
_BMP_FILE_HEADER_BYTES = 14
_BMP_DIB_HEADER_SIZES = frozenset({12, 16, 40, 52, 56, 64, 108, 124})


def _is_bmp_prefix(data: bytes) -> bool:
    """Return whether ``data`` proves a structurally plausible BMP header."""
    if len(data) < IMAGE_SNIFF_BYTES or not data.startswith(b"BM"):
        return False
    declared_size = int.from_bytes(data[2:6], "little")
    reserved = data[6:10]
    pixel_offset = int.from_bytes(data[10:14], "little")
    dib_header_size = int.from_bytes(data[14:18], "little")
    return (
        reserved == b"\x00" * 4
        and dib_header_size in _BMP_DIB_HEADER_SIZES
        and pixel_offset >= _BMP_FILE_HEADER_BYTES + dib_header_size
        and declared_size > pixel_offset
    )


def image_mime_from_prefix(data: bytes) -> str | None:
    """Return the supported image MIME proven by a file's leading bytes."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if _is_bmp_prefix(data):
        return "image/bmp"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return _HEIF_BRAND_MIME.get(data[8:12])
    return None
