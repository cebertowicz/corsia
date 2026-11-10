"""Read-only Gmail calls for search and read: headers and bodies, never Gmail's snippet.

Gmail's ``snippet`` is body text, so no call here asks for it or returns it.
"""

from __future__ import annotations

import asyncio
import base64
import html
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from connections.google import client
from connections.google.email_gmail import GMAIL
from connections.mail.conversation import ConversationEmail, header_addresses
from connections.mail.received_attachments import Attachment

log = logging.getLogger(__name__)

API = f"{GMAIL}/messages"
MAX_RESULTS = 25
HEADER_NAMES = ("From", "To", "Cc", "Date", "Subject")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TAGS = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.DOTALL | re.IGNORECASE)
_BLANKS = re.compile(r"\n{3,}")


class GmailRequestError(client.GoogleError):
    """The request was refused before it left; the text is safe to show."""


@dataclass(frozen=True)
class Message:
    message_id: str
    thread_id: str
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    attachments: tuple[Attachment, ...] = ()


def check_message_id(value: object, _account_id: str | None = None) -> str:
    """The id as Gmail issues them, or a fixed refusal; keeps a model-supplied value out of the URL path.

    Gmail's ids are already unique to a mailbox; the account is taken so every mail backend answers one call.
    """
    if not isinstance(value, str) or not _ID.match(value.strip()):
        raise GmailRequestError("That message can't be found.")
    return value.strip()


def headers_from_payload(payload: dict[str, Any]) -> dict[str, str]:
    """Keep the permitted Gmail metadata headers in the lower-case tool shape."""
    found: dict[str, str] = {}
    for item in payload.get("headers") or []:
        name = str(item.get("name", "")).lower()
        if name in {n.lower() for n in HEADER_NAMES} and name not in found:
            found[name] = str(item.get("value", ""))
    return found


async def search(account_id: str, query: str, max_results: int) -> list[Message]:
    """One account's matching messages' ids and headers only, newest first (Gmail's order)."""
    limit = max(1, min(int(max_results), MAX_RESULTS))
    token = await client.access_token(account_id)
    listed = await client.request_json(
        "GET", API, account_id=account_id, token=token,
        params={"q": query, "maxResults": limit, "fields": "messages(id,threadId)"},
    )
    refs = [m for m in (listed.get("messages") or []) if _ID.match(str(m.get("id", "")))][:limit]

    async def one(ref: dict[str, Any]) -> Message:
        data = await client.request_json(
            "GET", f"{API}/{ref['id']}", account_id=account_id, token=token,
            params={
                "format": "metadata", "metadataHeaders": ["From", "Date", "Subject"],
                "fields": "id,threadId,payload(headers)",
            },
        )
        return Message(str(ref["id"]), str(data.get("threadId") or ref.get("threadId") or ""),
                       headers_from_payload(data.get("payload") or {}))

    return list(await asyncio.gather(*(one(ref) for ref in refs)))


def _decode_bytes(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _decode(data: str) -> str:
    return _decode_bytes(data).decode("utf-8", "replace")


def parts(payload: dict[str, Any], mime: str) -> list[str]:
    out: list[str] = []
    if payload.get("mimeType") == mime and (payload.get("body") or {}).get("data"):
        out.append(_decode(payload["body"]["data"]))
    for part in payload.get("parts") or []:
        out.extend(parts(part, mime))
    return out


def body_text(payload: dict[str, Any]) -> str:
    """The message's plain text, or its HTML with tags removed when there is no plain part."""
    plain = parts(payload, "text/plain")
    if plain:
        return "\n".join(plain).strip()
    rendered = [html.unescape(_TAGS.sub(" ", chunk)) for chunk in parts(payload, "text/html")]
    return _BLANKS.sub("\n\n", "\n".join(rendered)).strip()


def _attached(part: dict[str, Any]) -> bool:
    """A part with a filename is an attachment, as is one marked ``attachment``; an inline image has neither."""
    if part.get("filename"):
        return True
    disposition = next((h.get("value", "") for h in part.get("headers") or []
                        if str(h.get("name", "")).lower() == "content-disposition"), "")
    return disposition.strip().lower().startswith("attachment")


def attachments_of(payload: dict[str, Any]) -> tuple[Attachment, ...]:
    """The message's attachments in order, each with Gmail's ``attachmentId`` or its inline bytes."""
    found: list[Attachment] = []
    _collect(payload, found)
    return tuple(found)


def _collect(part: dict[str, Any], found: list[Attachment]) -> None:
    children = part.get("parts") or []
    for child in children:
        _collect(child, found)
    if children or not _attached(part):
        return
    body = part.get("body") or {}
    data = None
    if not body.get("attachmentId"):
        data = _decode_bytes(body["data"]) if body.get("data") else b""
    size = len(data) if data is not None else int(body.get("size") or 0)
    found.append(Attachment(str(part.get("filename") or ""), str(part.get("mimeType") or ""), size,
                            str(body.get("attachmentId") or ""), data))


async def attachment_bytes(account_id: str, message_id: str, attachments: Sequence[Attachment]) -> list[bytes | None]:
    """Each attachment's bytes (None for one Gmail would not give), from the message just read."""
    mid = check_message_id(message_id)
    blobs: list[bytes | None] = []
    for item in attachments:
        if item.data is not None:
            blobs.append(item.data)
            continue
        try:
            got = await client.request_json(
                "GET", f"{API}/{mid}/attachments/{quote(item.ref, safe='')}", account_id=account_id,
                params={"fields": "data"},
            )
            blobs.append(_decode_bytes(got["data"]))
        except Exception:  # noqa: BLE001 - this one is reported in its own note
            log.exception("Gmail attachment download failed")
            blobs.append(None)
    return blobs


async def fetch(account_id: str, message_id: str) -> Message:
    """One message of one account, with its headers and decoded body."""
    mid = check_message_id(message_id)
    data = await client.request_json(
        "GET", f"{API}/{mid}", account_id=account_id,
        params={"format": "full", "fields": "id,threadId,payload"},
    )
    payload = data.get("payload") or {}
    return Message(mid, str(data.get("threadId") or ""), headers_from_payload(payload), body_text(payload),
                   attachments_of(payload))


async def conversation(account_id: str, thread_id: str) -> list[ConversationEmail]:
    """Every email of one Gmail thread with its text, in one ``threads.get`` call; the account's drafts are left out."""
    if not isinstance(thread_id, str) or not _ID.match(thread_id):
        raise GmailRequestError("That conversation can't be found.")
    data = await client.request_json(
        "GET", f"{GMAIL}/threads/{quote(thread_id, safe='')}", account_id=account_id,
        params={"format": "full", "fields": "messages(id,labelIds,internalDate,payload)"},
    )
    emails = []
    for raw in data.get("messages") or []:
        if "DRAFT" in (raw.get("labelIds") or []):
            continue
        payload = raw.get("payload") or {}
        headers = headers_from_payload(payload)
        # Reply-To isn't one of the tool-shape headers; the view needs it only as an address.
        reply_to = next((str(h.get("value", "")) for h in payload.get("headers") or []
                         if isinstance(h, dict) and str(h.get("name", "")).lower() == "reply-to"), "")
        emails.append(ConversationEmail(
            message_id=str(raw["id"]), sender=headers.get("from", ""),
            date=datetime.fromtimestamp(int(raw["internalDate"]) / 1000, timezone.utc), body=body_text(payload),
            addresses=header_addresses(headers.get("from", ""), headers.get("to", ""), headers.get("cc", ""), reply_to),
        ))
    return emails
