"""Provider read adapters for the shared mail lifecycle; no scheduling or writes."""
from __future__ import annotations

from collections.abc import AsyncIterator
from email.utils import parsedate_to_datetime
from typing import Any

from connections.mail import email_screen
from connections.google.accounts import thread_key as google_thread_key
from connections.microsoft import client as microsoft_client, mail as microsoft_mail
from connections.microsoft.mail import thread_key as microsoft_thread_key
from connections.mailhost import inbox as mailhost_inbox
from events.connectors import gmail_feed as feed


class GmailRead:
    connector = "gmail"
    cursor_key = "history_id"
    thread_key = staticmethod(google_thread_key)

    async def floor(self, account_id: str) -> str:
        return await feed.profile_history_id(account_id)

    async def read(self, account_id: str, position: str) -> tuple[str, Any]:
        page = await feed.added_messages(account_id, position)
        return page.history_id, page

    async def messages(self, account_id: str, page: Any) -> AsyncIterator[tuple[Any, Any]]:
        for message_id in page.message_ids:
            try:
                yield await feed.metadata(account_id, message_id), await feed.full_message(account_id, message_id)
            except feed.GmailMessageGone:
                continue

    def date(self, headers: dict[str, str]) -> str | None:
        try:
            parsed = parsedate_to_datetime(headers.get("date", ""))
            return parsed.isoformat() if parsed is not None else None
        except (TypeError, ValueError):
            return None

    def cursor_facts(self, position: str) -> dict[str, str]:
        return {"history_id": position}


class MicrosoftRead:
    connector = "microsoft"
    cursor_key = "delta_link"
    thread_key = staticmethod(microsoft_thread_key)

    async def floor(self, account_id: str) -> str:
        # https://learn.microsoft.com/en-us/graph/api/message-delta?view=graph-rest-1.0
        # Graph says to "copy and apply the returned URL to the next delta function call";
        # it already includes the requested parameters in encoded form.
        from infra.time import utc_now_iso

        _, delta_link = await microsoft_client.graph_delta(
            "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta",
            account_id=account_id,
            params={
                "$filter": f"receivedDateTime ge {utc_now_iso()}",
                "$select": microsoft_mail.MESSAGE_FIELDS,
            },
        )
        return delta_link

    async def read(self, account_id: str, position: str) -> tuple[str, Any]:
        values, next_position = await microsoft_client.graph_delta(position, account_id=account_id)
        messages = [microsoft_mail._message(item) for item in values if "@removed" not in item]
        return next_position, messages

    async def messages(self, account_id: str, page: Any) -> AsyncIterator[tuple[Any, Any]]:
        for message in page:
            yield message, message

    def date(self, headers: dict[str, str]) -> str | None:
        return headers.get("date") or None

    def cursor_facts(self, position: str) -> dict[str, str]:
        # A Graph delta URL can contain opaque credentials; it never enters events.
        return {}


class MailHostRead:
    connector = "mailhost"
    cursor_key = "imap_uid"

    @staticmethod
    def thread_key(_account_id: str, _message_thread_id: object) -> None:
        return None

    async def floor(self, account_id: str) -> str:
        return await mailhost_inbox.floor(account_id)

    async def read(self, account_id: str, position: str) -> tuple[str, list[Any]]:
        return await mailhost_inbox.added(account_id, position)

    async def messages(self, account_id: str, page: Any) -> AsyncIterator[tuple[Any, Any]]:
        for message in page:
            yield message, message

    def date(self, headers: dict[str, str]) -> str | None:
        return headers.get("date") or None

    def cursor_facts(self, position: str) -> dict[str, str]:
        return {}


ADAPTERS: dict[str, GmailRead | MicrosoftRead | MailHostRead] = {
    "google": GmailRead(), "microsoft": MicrosoftRead(), "mailhost": MailHostRead(),
}


def event_payload(
    account_id: str, message: Any, screen: email_screen.Screen, history_id: str,
    provider: str = "google",
) -> dict[str, Any]:
    """Flat email facts with screened subject only; bodies never enter events."""
    adapter = ADAPTERS[provider]
    headers = message.headers
    payload: dict[str, Any] = {
        "account_id": account_id,
        "message_id": message.message_id,
        "thread_id": adapter.thread_key(account_id, message.thread_id),
        **adapter.cursor_facts(history_id),
        "from": headers.get("from", ""), "to": headers.get("to", ""), "label": "INBOX",
        "screen_status": screen.status, "needs_person": screen.needs_person,
        "injection": bool(screen.injection), "injection_rule": screen.rule,
        "kind": screen.kind, "screen_model": screen.model_id,
    }
    if date := adapter.date(headers):
        payload["date"] = date
    if screen.clean and (subject := headers.get("subject")):
        payload["subject"] = subject
    return payload
