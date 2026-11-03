"""The note a chat attachment leaves in the agent's input, for every harness.

One writer: ``infra.input_render`` appends these lines to the current user input,
so native, Claude Code and Codex receive the identical text exactly once and
preflight measures what dispatch sends.

A document or other file also names where it is saved, relative to the agent's
working directory (the thread's project root, or the workspace root outside a
project), so ``read_file`` opens it as written. Pixels travel separately, so an
image keeps its bare note.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from core.workspace_layout import uploads_root_for_path
from infra.config import DOCUMENT_MIME_TYPES

log = logging.getLogger(__name__)


def _saved_path(file_id: object, thread_id: str, db=None) -> str | None:
    """Return the registered upload's path under its scope root, or ``None``.

    Never built from the filename: the path comes from the upload registry and
    must sit in this thread's own uploads root, so a legacy upload, a missing
    file or another thread's file yields no path.
    """
    if not isinstance(file_id, str) or not file_id:
        return None
    from registry.registration_events import resolve_upload

    try:
        artifact = resolve_upload(file_id, db=db)
        raw = artifact.get("path") if artifact else None
        if not raw:
            return None
        path = Path(raw).resolve(strict=False)
        uploads = uploads_root_for_path(path)
        if uploads is None or uploads.parent.name != thread_id or not path.is_file():
            return None
        return path.relative_to(uploads.parent.parent.parent).as_posix()
    except (sqlite3.Error, OSError, ValueError, TypeError) as error:
        # Never raise into Turn construction, but an unexpected failure is not a
        # missing upload: say so, without the path or filename.
        log.warning(
            "attachment path lookup failed file_id=%s error=%s",
            file_id, type(error).__name__,
        )
        return None


def _mime(attachment: dict) -> str:
    mime = attachment.get("mime_type")
    return mime if isinstance(mime, str) else ""


def attachment_notes(attachments: list | None, thread_id: str, db=None) -> str:
    """Return one note line per attachment; tolerant of anything on the wire."""
    lines: list[str] = []
    for attachment in attachments or ():
        if not isinstance(attachment, dict):
            continue
        mime = _mime(attachment)
        name = attachment.get("filename", "file")
        if mime.startswith("image/"):
            lines.append(f"[Image attached: {name}]")
            continue
        label = "Document" if mime in DOCUMENT_MIME_TYPES else "File"
        saved = _saved_path(attachment.get("file_id"), thread_id, db)
        where = f", saved at {saved}" if saved else ""
        lines.append(f"[{label} attached: {name}{where}]")
    return "\n".join(lines)


async def attachment_notes_async(attachments: list | None, thread_id: str) -> str:
    """``attachment_notes``, hopping to the DB pool only when a path lookup can happen."""
    if any(
        isinstance(item, dict) and not _mime(item).startswith("image/")
        for item in attachments or ()
    ):
        from infra.db_async import run_in_db

        return await run_in_db(attachment_notes, attachments, thread_id)
    return attachment_notes(attachments, thread_id)


def with_attachment_notes(content: str, notes: str) -> str:
    """Append the notes to the current user input."""
    if not notes:
        return content
    return f"{content}\n\n{notes}" if content else notes


def store_attachment_notes(batch, notes: str, db=None) -> None:
    """Keep the note block a provider message carried on the batch's last row.

    Replay appends it to that row's visible content, so the replayed message is
    the live one without reading the upload registry again.
    """
    if not notes:
        return
    from infra.database import write_transaction

    with write_transaction(db, scope="store_attachment_notes") as connection:
        connection.execute(
            "UPDATE messages SET metadata=json_set(COALESCE(metadata,'{}'),"
            "'$.attachment_notes',?) WHERE id=?",
            (notes, batch.items[-1].message_id),
        )
