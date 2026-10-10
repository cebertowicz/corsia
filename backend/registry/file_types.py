"""File types of a workspace artifact, by file extension.

One table for classification and for serving image bytes, so the type an
artifact is given and the Content-Type its file is served with cannot drift.
SVG is served as ``image/svg+xml`` and only ever shown through ``<img>``.
"""

from __future__ import annotations

from pathlib import Path

DOCUMENT_EXTENSIONS = {".md", ".txt", ".markdown", ".rst"}

IMAGE_MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}


def image_media_type(path: Path) -> str | None:
    """The media type for an image path (case-insensitive), else ``None``."""
    return IMAGE_MEDIA_TYPES.get(path.suffix.lower())


def extension_type(path: Path) -> str:
    """``image``, ``document`` or ``code`` for a file, by extension (case-insensitive)."""
    if image_media_type(path) is not None:
        return "image"
    return "document" if path.suffix.lower() in DOCUMENT_EXTENSIONS else "code"
