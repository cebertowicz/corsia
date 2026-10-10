"""Admit an image or video read as an immutable reference, never as bytes.

A media read produces two things: a short canonical placeholder that is safe to
store, index, and checkpoint forever, and a reference describing where the bytes
live. Bytes are resolved once, at a provider boundary, by ``core.media_delivery``.
Admission may verify a container, but nothing here encodes, resizes, or samples it.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, BinaryIO

from core.media_sniffing import IMAGE_SNIFF_BYTES, image_mime_from_prefix
from infra.config import (
    MAX_IMAGE_UPLOAD_BYTES,
    MAX_VIDEO_UPLOAD_BYTES,
    VIDEO_MIME_TYPES,
)

IMAGE_KIND = "image"
VIDEO_KIND = "video"

# Enough for every image signature and the 12-byte video signatures below.
_SNIFF_BYTES = IMAGE_SNIFF_BYTES
_DIGEST_CHUNK = 1024 * 1024
_EBML_MAGIC = b"\x1a\x45\xdf\xa3"
_MP4_VIDEO_BRANDS = {
    b"avc1",
    b"dash",
    b"iso2",
    b"iso3",
    b"iso4",
    b"iso5",
    b"iso6",
    b"isom",
    b"mp41",
    b"mp42",
}
_QUICKTIME_BRANDS = {b"qt  "}

_MEDIA_LIMITS = {
    IMAGE_KIND: MAX_IMAGE_UPLOAD_BYTES,
    VIDEO_KIND: MAX_VIDEO_UPLOAD_BYTES,
}


@dataclass(frozen=True, slots=True)
class MediaReadReference:
    """One immutable description of media a tool read, carrying no bytes."""

    path: str
    mime_type: str
    kind: str
    byte_size: int
    sha256: str

    @property
    def filename(self) -> str:
        """Return the leaf name shown to the model and the user."""
        return Path(self.path).name

    def as_metadata(self) -> dict[str, Any]:
        """Render the reference for canonical tool-result metadata."""
        return {
            "path": self.path,
            "mime_type": self.mime_type,
            "kind": self.kind,
            "byte_size": self.byte_size,
            "sha256": self.sha256,
        }

    def placeholder_text(self) -> str:
        """Return the short canonical text that stands in for the bytes."""
        label = "Image" if self.kind == IMAGE_KIND else "Video"
        return (
            f"[{label}: {self.filename} — {self.mime_type}, "
            f"{_human_bytes(self.byte_size)}]"
        )


class MediaToolResult(str):
    """A tool result that is text to every consumer and media to the boundary.

    ``Tool.execute`` stays string-compatible: every existing caller sees exactly
    the placeholder text. Only the provider boundaries look for ``media``.
    """

    media: MediaReadReference

    def __new__(cls, value: str, media: MediaReadReference) -> "MediaToolResult":
        instance = super().__new__(cls, value)
        instance.media = media
        return instance

    def __getnewargs__(self) -> tuple[str, MediaReadReference]:
        """Preserve the media constructor argument across copy/pickle."""
        return str(self), self.media


def _human_bytes(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f}MB"
    if size >= 1024:
        return f"{size / 1024:.1f}KB"
    return f"{size}B"


def _limit_label(kind: str) -> str:
    return f"{_MEDIA_LIMITS[kind] // (1024 * 1024)}MB"


def media_reference_from_metadata(
    metadata: dict[str, Any] | None,
) -> MediaReadReference | None:
    """Rebuild a reference from canonical metadata, or None when absent."""
    payload = (metadata or {}).get("media")
    if not isinstance(payload, dict):
        return None
    try:
        return MediaReadReference(
            path=str(payload["path"]),
            mime_type=str(payload["mime_type"]),
            kind=str(payload["kind"]),
            byte_size=int(payload["byte_size"]),
            sha256=str(payload["sha256"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _video_mime_from_prefix(data: bytes, path: Path) -> str | None:
    """Return the video MIME proven by container magic bytes, if any."""
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in _QUICKTIME_BRANDS:
            return "video/quicktime"
        if brand in _MP4_VIDEO_BRANDS:
            return "video/mp4"
        return None
    if data.startswith(_EBML_MAGIC):
        return (
            "video/x-matroska"
            if path.suffix.lower() == ".mkv"
            else "video/webm"
        )
    return None


def detect_media(
    header: bytes,
    path: Path,
    *,
    source: BinaryIO | None = None,
) -> tuple[str, str] | None:
    """Classify bytes as (kind, mime), or return None for an ordinary file."""
    image_mime = image_mime_from_prefix(header)
    if image_mime is not None:
        return IMAGE_KIND, image_mime
    video_mime = _video_mime_from_prefix(header, path)
    if video_mime is not None and video_mime in VIDEO_MIME_TYPES:
        return VIDEO_KIND, video_mime
    if source is not None:
        from core.media_image_validation import image_mime_from_stream

        image_mime = image_mime_from_stream(source)
        if image_mime is not None:
            return IMAGE_KIND, image_mime
    return None


def _digest(handle: Any) -> str:
    digest = sha256()
    handle.seek(0)
    while chunk := handle.read(_DIGEST_CHUNK):
        digest.update(chunk)
    return digest.hexdigest()


def media_read_reference(path: Path) -> MediaReadReference:
    """Build the reference for one media file, digesting its exact bytes.

    Raises ``ValueError`` when the file is not recognized media.
    """
    from tools.filesystem_binary import open_bound_binary

    with open_bound_binary(path) as (handle, file_stat):
        reference = _reference_from_open_file(handle, file_stat, path)
    if reference is None:
        raise ValueError(f"not recognized media: {path}")
    return reference


def _reference_from_open_file(
    handle: Any,
    file_stat: Any,
    path: Path,
) -> MediaReadReference | None:
    """Build a reference from an already-bound descriptor, or None for text."""
    detected = detect_media(handle.read(_SNIFF_BYTES), path, source=handle)
    if detected is None:
        return None
    kind, mime_type = detected
    return MediaReadReference(
        path=str(path),
        mime_type=mime_type,
        kind=kind,
        byte_size=file_stat.st_size,
        sha256=_digest(handle),
    )


def _oversize_message(kind: str, path: Path, size: int) -> str:
    label = "Image" if kind == IMAGE_KIND else "Video"
    return (
        f"Error: {label} {path.name} is {_human_bytes(size)}, above the "
        f"{_limit_label(kind)} {kind} read limit. Use a smaller "
        f"{kind}, or process it outside the conversation."
    )


# Admission stays provider-neutral; the delivery boundary owns capability.


def read_media_result(handle: Any, file_stat: Any, path: Path) -> str | None:
    """Admit one media read from a bound descriptor, or None when it is text.

    The caller owns the single bound open, so the authorized object is never
    re-resolved: a swap between two opens is exactly the attack that binding
    exists to prevent.

    Admitted media always returns a ``MediaToolResult`` carrying its typed
    reference and provider-neutral placeholder. Capability decisions belong to
    the delivery boundary. Media above its ceiling returns a plain ``Error:``
    string.
    """
    header = handle.read(_SNIFF_BYTES)
    detected = detect_media(header, path, source=handle)
    if detected is None:
        handle.seek(0)
        return None
    kind, mime_type = detected
    size = file_stat.st_size
    if size > _MEDIA_LIMITS[kind]:
        return _oversize_message(kind, path, size)
    reference = MediaReadReference(
        path=str(path),
        mime_type=mime_type,
        kind=kind,
        byte_size=size,
        sha256=_digest(handle),
    )
    return MediaToolResult(reference.placeholder_text(), reference)


__all__ = [
    "IMAGE_KIND",
    "VIDEO_KIND",
    "MediaReadReference",
    "MediaToolResult",
    "detect_media",
    "media_read_reference",
    "media_reference_from_metadata",
    "read_media_result",
]
