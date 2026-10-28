"""Gmail reads needed by the shared reply guard."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Any
from urllib.parse import quote

from connections.google import client, gmail_read
from connections.google.email_gmail import GMAIL
from connections.mail.email_document import DraftRefused, reply_addresses
from tools.mail_guard import Message


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    return {str(h.get("name", "")).lower(): str(h.get("value", ""))
            for h in payload.get("headers", []) if isinstance(h, dict)}


def _message(raw: dict[str, Any]) -> Message:
    payload = raw.get("payload") or {}
    headers = _headers(payload)
    return Message(message_id=str(raw["id"]), thread_id=str(raw.get("threadId") or ""),
                   sender=headers.get("from", ""), subject=headers.get("subject", ""),
                   date=datetime.fromtimestamp(int(raw["internalDate"]) / 1000, timezone.utc),
                   body="", reply_to=headers.get("reply-to", ""),
                   unread="UNREAD" in (raw.get("labelIds") or []))


async def _full(account_id: str, message_id: str) -> dict[str, Any]:
    mid = gmail_read.check_message_id(message_id)
    return await client.request_json("GET", f"{GMAIL}/messages/{quote(mid, safe='')}",
                                     account_id=account_id, params={"format": "full"})


async def _metadata(account_id: str, message_id: str) -> dict[str, Any]:
    mid = gmail_read.check_message_id(message_id)
    return await client.request_json("GET", f"{GMAIL}/messages/{quote(mid, safe='')}",
                                     account_id=account_id, params={"format": "metadata"})


async def _search(account_id: str, query: str, limit: int | None = None) -> list[str]:
    ids: list[str] = []
    page: str | None = None
    while True:
        params: dict[str, Any] = {"q": query, "maxResults": limit or 500}
        if page:
            params["pageToken"] = page
        result = await client.request_json("GET", f"{GMAIL}/messages", account_id=account_id, params=params)
        ids.extend(str(item["id"]) for item in result.get("messages", []))
        page = result.get("nextPageToken")
        if not page or (limit is not None and len(ids) >= limit):
            return ids[:limit] if limit is not None else ids


async def guard_data(account_id: str, message_id: str, since: datetime | None = None) -> tuple[Message, list[Message], list[Message], int]:
    target = _message(await _metadata(account_id, message_id))
    if not target.thread_id:
        raise DraftRefused("The original message has no thread. Send a new email instead.")
    raw_thread = await client.request_json("GET", f"{GMAIL}/threads/{quote(target.thread_id, safe='')}",
                                           account_id=account_id, params={"format": "metadata"})
    thread = [_message(item) for item in raw_thread.get("messages", []) if "DRAFT" not in item.get("labelIds", [])]
    _, address = parseaddr(target.sender)
    if not address:
        recipients = reply_addresses(target.reply_to, '')
        if recipients:
            raise DraftRefused(f"This message has no sender address, so later mail from them can't be checked. Write a new email to {recipients[0]} instead.")
        raise DraftRefused("The original message has no usable reply address. Send a new email to the correct address instead.")
    cutoff = since.replace(microsecond=0) if since is not None else target.date
    ids = await _search(account_id, f"from:{address} after:{int(cutoff.timestamp()) - 1}")
    count = len(set(await _search(account_id, f"from:{address} newer_than:30d"))) if since is None else 0
    by_id = {item.message_id: item for item in thread}
    for mid in sorted(set(ids).difference(by_id)):
        raw = await _metadata(account_id, mid)
        if "DRAFT" not in raw.get("labelIds", []):
            by_id[str(raw["id"])] = _message(raw)
    sender = [by_id[mid] for mid in ids if mid in by_id
              and parseaddr(by_id[mid].sender)[1].casefold() == address.casefold()]
    if since is not None:
        thread = [item for item in thread if item.message_id != target.message_id and item.date >= cutoff]
        sender = [item for item in sender if item.message_id != target.message_id and item.date >= cutoff]
    else:
        sender = [item for item in sender if item.date >= cutoff]
    return target, thread, sender, count


async def sent_html(account_id: str) -> list[str]:
    ids = await _search(account_id, "in:sent", limit=5)
    messages = [await _full(account_id, mid) for mid in ids]
    return ["\n".join(gmail_read.parts(raw.get("payload") or {}, "text/html")) for raw in messages]
