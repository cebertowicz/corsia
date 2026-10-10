"""Safe, bounded reads of document text used by comment anchoring."""

from __future__ import annotations

from pathlib import Path


MAX_DOC_BYTES = 1_000_000


def read_doc_text(path: str | None) -> str:
    """Return readable UTF-8 document text, or raise ``ValueError``.

    Comment creation turns these failures into its existing endpoint-specific
    errors. Read-time anchoring treats them as a skipped check instead.
    """
    if not path:
        raise ValueError("target has no readable doc content to anchor a quote")
    document = Path(path)
    from doceditor.live_tools import find_live
    entry = find_live(document)
    if entry is not None:
        # A plain immutable snapshot is safe for comment adapters on DB workers;
        # pycrdt remains confined to its owning event loop.
        return entry.live.snapshot[0]
    if not document.is_file():
        raise ValueError(f"doc not found on disk: {path}")
    if document.stat().st_size > MAX_DOC_BYTES:
        raise ValueError("doc too large to anchor against")
    raw = document.read_bytes()
    if b"\x00" in raw[:8192]:
        raise ValueError("binary file cannot be comment-anchored")
    return raw.decode("utf-8", errors="replace")
