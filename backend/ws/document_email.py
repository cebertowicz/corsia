"""The email draft header on live-document frames, so the draft view's warnings follow every edit.

An open document is an email draft when its load found the draft record (``api/files_load.py`` ``email_draft``);
its frames then carry the header as the server reads it, from the live text the frame describes.
"""

from __future__ import annotations

from connections.mail.email_document import email_draft_view

_drafts: dict[str, str] = {}


def remember(artifact_id: str, loaded: dict) -> None:
    if loaded.get("email_draft"):
        _drafts[artifact_id] = loaded["email_draft"]["id"]
    else:
        _drafts.pop(artifact_id, None)


def header(artifact_id: str, live) -> dict:
    """The frame's ``email_draft`` field; the live text is read only for a draft, so other documents' frames cost nothing."""
    draft_id = _drafts.get(artifact_id)
    return {"email_draft": email_draft_view(draft_id, live.text)} if draft_id else {}
