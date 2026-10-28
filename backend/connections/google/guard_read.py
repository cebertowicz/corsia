"""Gmail reads needed by the shared reply guard."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from typing import Any
from urllib.parse import quote

from connections.google import client, gmail_read
from connections.google.email_gmail import GMAIL
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


async def guard_data(account_id: str, message_id: str) -> tuple[Message, list[Message], list[Message]]:
    target = _message(await _metadata(account_id, message_id))
    if not target.thread_id:
        raise ValueError("The target has no Gmail thread.")
    raw_thread = await client.request_json("GET", f"{GMAIL}/threads/{quote(target.thread_id, safe='')}",
                                           account_id=account_id, params={"format": "metadata"})
    thread = [_message(item) for item in raw_thread.get("messages", []) if "DRAFT" not in item.get("labelIds", [])]
    name, address = parseaddr(target.sender)
    # The guard applies exact timestamps after these inclusive provider searches.
    cutoffs = {target.date, datetime.now(timezone.utc) - timedelta(days=30)}
    senders = []
    if name:
        senders.append(f'from:"{name.replace(chr(34), "").replace(chr(92), "")}"')
    if address:
        senders.append(f"from:{address}")
    ids: set[str] = set()
    for cutoff in cutoffs:
        after = int(cutoff.timestamp()) - 1
        for sender in senders:
            ids.update(await _search(account_id, f"{sender} after:{after}"))
    by_id = {item.message_id: item for item in thread}
    missing = ids.difference(by_id)
    for mid in sorted(missing):
        raw = await _metadata(account_id, mid)
        if "DRAFT" not in raw.get("labelIds", []):
            by_id[str(raw["id"])] = _message(raw)
    return target, thread, [by_id[mid] for mid in ids if mid in by_id]


async def sent_html(account_id: str) -> list[str]:
    ids = await _search(account_id, "in:sent", limit=5)
    messages = [await _full(account_id, mid) for mid in ids]
    return ["\n".join(gmail_read._parts(raw.get("payload") or {}, "text/html")) for raw in messages]
