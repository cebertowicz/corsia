"""The one rule deciding whether a path may open as a live document now."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING

from doceditor.live_lane import LaneRefused
from doceditor.live_store import StoreRefused

if TYPE_CHECKING:
    from doceditor.live_runtime import DocumentLifetime, LiveEntry


class OpenRefused(LaneRefused):
    """A typed reason a live open was refused; the transport sends its kind."""

    def __init__(self, kind: str, message: str, *, title: str | None = None, blocking: bool = False):
        super().__init__(message)
        self.kind = kind
        self.title = title
        self.blocking = blocking

    def detail(self) -> dict[str, str]:
        """The ``{code, title, message}`` shape legacy saves and frames share."""
        return {"code": self.kind, "title": self.title or "", "message": str(self)}


_UNAVAILABLE_TITLE = "Live editing is unavailable for this document."
_STORE_COPY = {
    # Product Experience approved the damaged copy; it never offers Retry.
    "store_damaged": "Its saved editing history could not be read. Text in this tab is kept. Copy it to keep working.",
    "store_unavailable": "Its saved editing history can't be opened right now. Text in this tab is kept. "
                         "Retry, or copy it to keep working.",
}


def falls_back_to_plain(kind: str) -> bool:
    """True when a store refused as ``kind`` leaves its file to the plain, non-live writers.

    A damaged store cannot clear, so its document stays editable without live
    editing; an unavailable store may still clear and keeps plain writes refused.
    """
    return kind == "store_damaged"


def store_refusal(error: StoreRefused) -> OpenRefused:
    """Type a store that failed to open: SQLite corruption is damage, anything else may clear."""
    cause = error.__cause__
    name = getattr(cause, "sqlite_errorname", "") if isinstance(cause, sqlite3.Error) else ""
    text = str(error).lower()
    damaged = name.startswith(("SQLITE_CORRUPT", "SQLITE_NOTADB")) or "malformed" in text or "not a database" in text
    kind = "store_damaged" if damaged else "store_unavailable"
    return OpenRefused(kind, _STORE_COPY[kind], title=_UNAVAILABLE_TITLE, blocking=True)


def open_refusal(
    documents: Mapping[str, LiveEntry], artifact_id: str, path: Path, owner: DocumentLifetime,
) -> OpenRefused | None:
    """Return why ``artifact_id`` may not open at ``path`` now, or None to admit it.

    Called with the path's lifetime lock held. An admitted identity that is
    already open is reused by the caller.
    """
    if owner.closing is not None and not owner.closing.done():
        return OpenRefused("cleanup_running", "Document cleanup is still running; retry opening it")
    existing = documents.get(artifact_id)
    if existing is not None and existing.live.path != path:
        return OpenRefused("document_moved", "The document moved; close and reopen it")
    if existing is None and any(entry.live.path == path for entry in documents.values()):
        return OpenRefused("path_open_elsewhere",
                           "This path is already open under another document identity; close it before reopening")
    return None
