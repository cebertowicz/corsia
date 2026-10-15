"""The Gmail reads behind an email draft: reply-target headers and the signature.

Every URL is a fixed template with ids URL-encoded, and no message body is ever requested.
"""

from __future__ import annotations

import json
from urllib.parse import quote

from connections.google import client as google_client
from connections.google.client import GoogleError, GoogleRequestFailed
from connections.google.email_document import DraftRefused, reply_target_label, signature_text

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
SEND_AS_URL = f"{GMAIL}/settings/sendAs"
NO_MESSAGE = "That message can't be found."


def message_url(message_id: str) -> str:
    return f"{GMAIL}/messages/{quote(message_id, safe='')}"


async def reply_target(message_id: str) -> tuple[dict, str]:
    """Threading facts and the line the person sees, from the message's headers only.

    No body is requested. The line is display; the dict is what the send threads with.
    """
    try:
        found = await google_client.request_json(
            "GET", message_url(message_id),
            params={
                "format": "metadata",
                "metadataHeaders": ["Message-ID", "References", "From", "Subject"],
            },
        )
    except GoogleRequestFailed as exc:
        if exc.status == 404:
            raise DraftRefused(NO_MESSAGE) from None
        raise
    if not isinstance(found, dict):
        raise DraftRefused(NO_MESSAGE)
    headers = {
        str(item.get("name", "")).lower(): str(item.get("value", ""))
        for item in (found.get("payload") or {}).get("headers", []) if isinstance(item, dict)
    }
    message_header = headers.get("message-id", "").strip()
    references = " ".join(part for part in (headers.get("references", "").strip(), message_header) if part)
    target = {
        "reply_to": message_id,
        "thread_id": found.get("threadId"),
        "in_reply_to": message_header or None,
        "references": references or None,
    }
    return target, reply_target_label(headers.get("from"), headers.get("subject"))


async def kept_reply_label(existing: dict | None) -> str | None:
    """The line for an update that names no new reply target but still threads under the old.

    A message that can no longer be read must not block the person's edit.
    """
    try:
        kept = json.loads(existing["target_json"] or "{}").get("reply_to") if existing else None
    except (TypeError, ValueError, AttributeError):
        kept = None
    if not kept:
        return None
    try:
        return (await reply_target(str(kept)))[1]
    except (DraftRefused, GoogleError):
        return reply_target_label(None, None)


async def fetch_signature() -> str:
    found = await google_client.get_json(SEND_AS_URL)
    aliases = found.get("sendAs") if isinstance(found, dict) else None
    if not isinstance(aliases, list):
        return ""
    chosen = next(
        (alias for alias in aliases if isinstance(alias, dict) and alias.get("isPrimary")),
        next((alias for alias in aliases if isinstance(alias, dict)), None),
    )
    html = chosen.get("signature") if chosen else None
    return signature_text(html) if isinstance(html, str) and html.strip() else ""
