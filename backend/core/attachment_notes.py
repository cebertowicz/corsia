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

from pathlib import Path

from core.workspace_layout import (
    resolve_project_root,
    resolve_thread_uploads_root,
    resolve_workspace_root,
)
from infra import config
from infra.config import DOCUMENT_MIME_TYPES


def _saved_path(attachment: dict, thread_id: str, db=None) -> str | None:
    """Return the registered upload's path under the thread's scope root, or ``None``.

    The upload route's and image projection's rule: the registry names this
    thread as the upload's parent, and the file sits in this thread's uploads
    root under its current scope. A legacy, missing, renamed or other thread's
    upload yields no path; a registry or layout failure raises like any other
    read while the input renders.
    """
    file_id = attachment.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        return None
    from infra.queries import get_thread
    from registry.registration_events import resolve_upload

    artifact = resolve_upload(file_id, db=db)
    if not artifact or artifact.get("parent_thread_id") != thread_id or not artifact.get("path"):
        return None
    thread = get_thread(thread_id, db=db)
    if thread is None:
        return None
    scope = (
        resolve_project_root(config.WORKSPACE, thread["project_id"], db=db)
        if thread["project_id"]
        else resolve_workspace_root(config.WORKSPACE)
    )
    if scope is None:
        return None
    scope = scope.resolve(strict=False)
    uploads = resolve_thread_uploads_root(scope, thread_id).resolve(strict=False)
    path = Path(artifact["path"]).resolve(strict=False)
    if (
        path.name != attachment.get("filename")
        or not path.is_relative_to(uploads)
        or not path.is_file()
    ):
        return None
    return path.relative_to(scope).as_posix()


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
        saved = _saved_path(attachment, thread_id, db)
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


def commit_attachment_notes(connection, batch, notes: str) -> None:
    """Keep the note block a provider message carried on the batch's last row.

    Runs inside the write fence's transaction: every row first drops any note an
    earlier, released attempt left, then the last row takes this batch's. Replay
    appends it to that row's visible content, so the replayed message is the
    live one without reading the upload registry again.
    """
    ids = [item.message_id for item in batch.items]
    marks = ",".join("?" * len(ids))
    connection.execute(
        "UPDATE messages SET metadata=json_remove(metadata,'$.attachment_notes') "
        f"WHERE id IN ({marks}) AND json_extract(metadata,'$.attachment_notes') IS NOT NULL",
        ids,
    )
    if notes:
        connection.execute(
            "UPDATE messages SET metadata=json_set(COALESCE(metadata,'{}'),"
            "'$.attachment_notes',?) WHERE id=?",
            (notes, ids[-1]),
        )
