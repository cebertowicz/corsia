"""Read-only Gmail API reads for the platform poller.

This module owns the Gmail history protocol only. It reuses the platform Google
client for the saved credential and ``gmail_read`` for message parsing, and it
never delivers an event or changes Gmail state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from connections.google import client
from connections.google import gmail_read
from connections.google.email_gmail import GMAIL

_HISTORY_URL = f"{GMAIL}/history"
_PROFILE_URL = f"{GMAIL}/profile"


class GmailFeedError(RuntimeError):
    """A poller read failed; ``error_class`` is safe for status and logs."""

    def __init__(self, error_class: str) -> None:
        super().__init__(error_class)
        self.error_class = error_class


class GmailHistoryExpired(GmailFeedError):
    """Gmail no longer retains the requested history cursor."""

    def __init__(self) -> None:
        super().__init__("history_expired")


class GmailMessageGone(GmailFeedError):
    """The message was deleted after the history listed it; the page skips it."""

    def __init__(self) -> None:
        super().__init__("message_http_404")


def _message_error(exc: client.GoogleRequestFailed) -> GmailFeedError:
    if exc.status == 404:
        return GmailMessageGone()
    return GmailFeedError(f"message_http_{exc.status}" if exc.status else "message_unreachable")


@dataclass(frozen=True)
class HistoryPage:
    """Unread Gmail message ids and the newest history id that covers them."""

    message_ids: tuple[str, ...]
    history_id: str


def _history_message_ids(raw: object) -> tuple[str, ...]:
    """Flatten Gmail's history records, preserving their API order and deduplicating ids."""
    found: list[str] = []
    seen: set[str] = set()
    histories = raw.get("history") if isinstance(raw, dict) else None
    for history in histories or ():
        if not isinstance(history, dict):
            continue
        for added in history.get("messagesAdded") or ():
            message = added.get("message") if isinstance(added, dict) else None
            message_id = message.get("id") if isinstance(message, dict) else None
            if isinstance(message_id, str) and message_id and message_id not in seen:
                seen.add(message_id)
                found.append(message_id)
    return tuple(found)


async def profile_history_id() -> str:
    """Read Gmail's current history floor for a first run or expired cursor."""
    try:
        profile = await client.get_json(_PROFILE_URL)
    except client.GoogleRequestFailed as exc:
        raise GmailFeedError(f"profile_http_{exc.status}" if exc.status else "profile_unreachable") from None
    history_id = profile.get("historyId") if isinstance(profile, dict) else None
    if not isinstance(history_id, str) or not history_id:
        raise GmailFeedError("profile_invalid")
    return history_id


async def added_messages(start_history_id: str) -> HistoryPage:
    """List every Inbox addition after one durable Gmail history cursor."""
    params: dict[str, str] = {
        "startHistoryId": start_history_id,
        "historyTypes": "messageAdded",
        "labelId": "INBOX",
    }
    message_ids: list[str] = []
    seen: set[str] = set()
    while True:
        try:
            raw = await client.get_json(_HISTORY_URL, params=params)
        except client.GoogleRequestFailed as exc:
            if exc.status == 404:
                raise GmailHistoryExpired() from None
            raise GmailFeedError(f"history_http_{exc.status}" if exc.status else "history_unreachable") from None
        page_history_id = raw.get("historyId") if isinstance(raw, dict) else None
        if not isinstance(page_history_id, str) or not page_history_id:
            raise GmailFeedError("history_invalid")
        for message_id in _history_message_ids(raw):
            if message_id not in seen:
                seen.add(message_id)
                message_ids.append(message_id)
        page_token = raw.get("nextPageToken") if isinstance(raw, dict) else None
        if not isinstance(page_token, str) or not page_token:
            return HistoryPage(tuple(message_ids), page_history_id)
        params["pageToken"] = page_token


async def metadata(message_id: str) -> gmail_read.Message:
    """Read only the event headers before a full message is needed for screening."""
    mid = gmail_read.check_message_id(message_id)
    try:
        raw = await client.get_json(
            f"{gmail_read.API}/{mid}",
            params={
                "format": "metadata",
                "metadataHeaders": ["From", "To", "Date", "Subject"],
                "fields": "id,threadId,labelIds,payload(headers)",
            },
        )
    except client.GoogleRequestFailed as exc:
        raise _message_error(exc) from None
    payload = raw.get("payload") if isinstance(raw, dict) else None
    if not isinstance(payload, dict):
        raise GmailFeedError("message_invalid")
    return gmail_read.Message(mid, str(raw.get("threadId") or ""), gmail_read.headers_from_payload(payload))


async def full_message(message_id: str) -> gmail_read.Message:
    """Read the full message only after metadata selects it for screening."""
    try:
        return await gmail_read.fetch(message_id)
    except client.GoogleRequestFailed as exc:
        raise _message_error(exc) from None
