"""Read-only Gmail calls for search and read: headers and bodies, never Gmail's snippet.

Gmail's ``snippet`` is body text, so no call here asks for it or returns it.
"""

from __future__ import annotations

import asyncio
import base64
import html
import re
from dataclasses import dataclass, field
from typing import Any

from connections.google import client
from connections.google.email_gmail import GMAIL

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


def check_message_id(value: object) -> str:
    """The id as Gmail issues them, or a fixed refusal; keeps a model-supplied value out of the URL path."""
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


def _decode(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", "replace")


def _parts(payload: dict[str, Any], mime: str) -> list[str]:
    out: list[str] = []
    if payload.get("mimeType") == mime and (payload.get("body") or {}).get("data"):
        out.append(_decode(payload["body"]["data"]))
    for part in payload.get("parts") or []:
        out.extend(_parts(part, mime))
    return out


def body_text(payload: dict[str, Any]) -> str:
    """The message's plain text, or its HTML with tags removed when there is no plain part."""
    plain = _parts(payload, "text/plain")
    if plain:
        return "\n".join(plain).strip()
    rendered = [html.unescape(_TAGS.sub(" ", chunk)) for chunk in _parts(payload, "text/html")]
    return _BLANKS.sub("\n\n", "\n".join(rendered)).strip()


async def fetch(account_id: str, message_id: str) -> Message:
    """One message of one account, with its headers and decoded body."""
    mid = check_message_id(message_id)
    data = await client.request_json(
        "GET", f"{API}/{mid}", account_id=account_id,
        params={"format": "full", "fields": "id,threadId,payload"},
    )
    payload = data.get("payload") or {}
    return Message(mid, str(data.get("threadId") or ""), headers_from_payload(payload), body_text(payload))
