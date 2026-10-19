"""Safe Microsoft Graph mail adapter; IDs stay scoped to the owning account."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from connections.microsoft import client
from connections.microsoft.errors import MicrosoftError, PROVIDER_UNAVAILABLE

MESSAGES = "https://graph.microsoft.com/v1.0/me/messages"


@dataclass(frozen=True)
class Message:
    message_id: str
    thread_id: str | None
    headers: dict[str, str]
    body: str


def _address(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    email = value.get("emailAddress")
    return str(email.get("address", "")) if isinstance(email, dict) else ""


def _message(value: object) -> Message:
    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
        raise MicrosoftError(PROVIDER_UNAVAILABLE)
    body_value = value.get("body")
    body = body_value.get("content", "") if isinstance(body_value, dict) else ""
    return Message(
        value["id"], value.get("conversationId") if isinstance(value.get("conversationId"), str) else None,
        {"from": _address(value.get("from")), "to": ", ".join(_address(x) for x in value.get("toRecipients", []) if isinstance(x, dict)),
         "date": str(value.get("receivedDateTime", "")), "subject": str(value.get("subject", ""))},
        body if isinstance(body, str) else "",
    )


async def search(account_id: str, query: str, maximum: int) -> list[Message]:
    pages = await client.graph_pages(
        MESSAGES, account_id=account_id,
        params={"$search": f'\"{query}\"', "$top": min(maximum, 25), "$select": "id,conversationId,from,toRecipients,receivedDateTime,subject,body"},
    )
    return [_message(item) for item in pages][:maximum]


async def fetch(account_id: str, message_id: str) -> Message:
    if not isinstance(message_id, str) or not message_id:
        raise MicrosoftError(PROVIDER_UNAVAILABLE)
    value = await client.graph_json("GET", f"{MESSAGES}/{quote(message_id, safe='')}", account_id=account_id)
    return _message(value)


async def send(account_id: str, recipients: list[str], subject: str, body: str) -> None:
    if not recipients or not all(isinstance(item, str) and item.strip() for item in recipients):
        raise MicrosoftError(PROVIDER_UNAVAILABLE)
    payload: dict[str, object] = {"message": {"subject": subject, "body": {"contentType": "Text", "content": body}, "toRecipients": [{"emailAddress": {"address": item}} for item in recipients]}}
    await client.graph_json("POST", f"{MESSAGES}/send", account_id=account_id, body=payload)
