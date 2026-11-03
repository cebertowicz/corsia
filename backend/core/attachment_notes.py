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

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from core.workspace_layout import resolve_project_root, resolve_workspace_root
from history.message_attachments import registered_upload_path
from infra import config
from infra.config import DOCUMENT_MIME_TYPES

if TYPE_CHECKING:
    from core.message_history import AgentEnvelopeRender


def _scope_root(thread_id: str, db) -> Path | None:
    """The thread's working directory: its project root, or the workspace root."""
    from infra.queries import get_thread

    thread = get_thread(thread_id, db=db)
    if thread is None:
        return None
    if thread["project_id"]:
        root = resolve_project_root(config.WORKSPACE, thread["project_id"], db=db)
        return root.resolve(strict=False) if root is not None else None
    return resolve_workspace_root(config.WORKSPACE)


def _relative_to(path: str, scope: Path | None) -> str | None:
    """``path`` relative to the working directory, or ``None`` when it lies outside it."""
    resolved = Path(path).resolve(strict=False)
    if scope is None or not resolved.is_relative_to(scope):
        return None
    return resolved.relative_to(scope).as_posix()


def _mime(attachment: dict) -> str:
    mime = attachment.get("mime_type")
    return mime if isinstance(mime, str) else ""


def _is_file(attachment: object) -> bool:
    return isinstance(attachment, dict) and not _mime(attachment).startswith("image/")


def attachment_notes(attachments: list | None, thread_id: str, db=None) -> str:
    """Return one note line per attachment; tolerant of anything on the wire.

    A file's saved path is the existing replay owner's lookup
    (``history.message_attachments.registered_upload_path``), shown relative to
    the thread's working directory. The thread and its working directory are
    read once, and only when a file (not an image) is attached. A registry or
    layout failure raises like any other read while the input renders.
    """
    owned = db is None and any(_is_file(item) for item in attachments or ())
    if owned:
        from infra.database import get_db

        db = get_db()
    try:
        scope: Path | None = None
        scoped = False
        lines: list[str] = []
        for attachment in attachments or ():
            if not isinstance(attachment, dict):
                continue
            name = attachment.get("filename", "file")
            if not _is_file(attachment):
                lines.append(f"[Image attached: {name}]")
                continue
            if not scoped:
                scope, scoped = _scope_root(thread_id, db), True
            registered = registered_upload_path(attachment.get("file_id"), thread_id, db=db)
            saved = _relative_to(registered, scope) if registered else None
            where = f", saved at {saved}" if saved else ""
            label = "Document" if _mime(attachment) in DOCUMENT_MIME_TYPES else "File"
            lines.append(f"[{label} attached: {name}{where}]")
        return "\n".join(lines)
    finally:
        if owned:
            db.close()


async def attachment_notes_async(attachments: list | None, thread_id: str) -> str:
    """``attachment_notes``, hopping to the DB pool only when a path lookup can happen."""
    if any(_is_file(item) for item in attachments or ()):
        from infra.db_async import run_in_db

        return await run_in_db(attachment_notes, attachments, thread_id)
    return attachment_notes(attachments, thread_id)


def with_attachment_notes(content: str, notes: str) -> str:
    """Append the notes to the current user input."""
    if not notes:
        return content
    return f"{content}\n\n{notes}" if content else notes


def envelope_with_notes(envelope: AgentEnvelopeRender, notes: str) -> AgentEnvelopeRender:
    """Append the notes to the envelope's content and to its body.

    The notes are part of the person's message, not CORSIA's framing, so content
    minus body stays the framing alone.
    """
    if not notes:
        return envelope
    return replace(
        envelope,
        content=with_attachment_notes(envelope.content, notes),
        body_content=with_attachment_notes(envelope.body_content, notes),
    )
