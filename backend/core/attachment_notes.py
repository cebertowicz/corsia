"""The note a chat attachment leaves in the agent's input, for every harness.

One writer: ``infra.input_render`` appends each FIFO member's notes to that
member's own text before it is framed, so native, Claude Code and Codex receive
the identical text exactly once and preflight measures what dispatch sends.

A document or other file also names where it is saved: the registered path the
replayed ``[CORSIA artifact references]`` block shows for the same upload, so
``read_file`` opens it from any working directory. Pixels travel separately, so
an image keeps its bare note.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from history.message_attachments import registered_upload_path
from infra.config import DOCUMENT_MIME_TYPES


def _mime(attachment: dict) -> str:
    mime = attachment.get("mime_type")
    return mime if isinstance(mime, str) else ""


def _is_file(attachment: object) -> bool:
    return isinstance(attachment, dict) and not _mime(attachment).startswith("image/")


def _note_lines(attachments: Sequence, thread_id: str, db) -> str:
    lines: list[str] = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        name = attachment.get("filename", "file")
        if not _is_file(attachment):
            lines.append(f"[Image attached: {name}]")
            continue
        saved = registered_upload_path(attachment.get("file_id"), thread_id, db=db)
        where = f", saved at {saved}" if saved else ""
        label = "Document" if _mime(attachment) in DOCUMENT_MIME_TYPES else "File"
        lines.append(f"[{label} attached: {name}{where}]")
    return "\n".join(lines)


def member_groups(items: Sequence, attachments: Sequence) -> list[list]:
    """Split a batch's validated attachments back into their members, in input order.

    ``public_attachments`` maps one to one, so each member owns the next
    ``len(its attachments)`` entries.
    """
    groups, start = [], 0
    for item in items:
        size = len(item.delivery_options.get("attachments") or ())
        groups.append(list(attachments[start:start + size]))
        start += size
    return groups


def member_notes(groups: Sequence[Sequence], thread_id: str, db=None) -> list[str]:
    """Return one note block per group of attachments; tolerant of anything on the wire.

    A file's saved path is the existing replay owner's lookup
    (``history.message_attachments.registered_upload_path``), shown as stored;
    only an unregistered or another thread's upload has none. The DB is opened
    once, and only when a file (not an image) is attached. A registry failure
    raises like any other read while the input renders.
    """
    owned = db is None and any(_is_file(item) for group in groups for item in group)
    if owned:
        from infra.database import get_db

        db = get_db()
    try:
        return [_note_lines(group, thread_id, db) for group in groups]
    finally:
        if owned:
            db.close()


def attachment_notes(attachments: list | None, thread_id: str, db=None) -> str:
    """One note line per attachment of a single input."""
    return member_notes([attachments or ()], thread_id, db)[0]


async def member_notes_async(groups: Sequence[Sequence], thread_id: str) -> list[str]:
    """``member_notes``, hopping to the DB pool only when a path lookup can happen."""
    if any(_is_file(item) for group in groups for item in group):
        from infra.db_async import run_in_db

        return await run_in_db(member_notes, groups, thread_id)
    return member_notes(groups, thread_id)


def with_attachment_notes(content: str, notes: str) -> str:
    """Append the notes to one input's text."""
    if not notes:
        return content
    return f"{content}\n\n{notes}" if content else notes


def noted_batch(batch, notes: Sequence[str]):
    """A rendering-only copy of the batch: each member's notes join its own text.

    The notes sit before the member's managed footer and numbering, so content,
    body and source all carry them. Callers keep the claimed batch for anything
    that persists or replays the person's message.
    """
    return replace(batch, items=tuple(
        replace(item, content=with_attachment_notes(item.content, note))
        for item, note in zip(batch.items, notes, strict=True)
    ))
