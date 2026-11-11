"""Server-read email headers on live frames, scoped to the live document's own lifetime."""
from __future__ import annotations

from connections.mail.email_document import email_draft_view
from doceditor.live_runtime import LiveEntry


def header(entry: LiveEntry) -> dict:
    """Identity is loaded from the draft row at open and retained only by this live entry."""
    return {"email_draft": email_draft_view(entry.email_draft_id, entry.live.text)} if entry.email_draft_id else {}
