"""Safe Microsoft Graph mail adapter; IDs stay scoped to the owning account."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from connections.microsoft import client
from connections.microsoft.errors import MicrosoftError, REQUEST_REFUSED, SERVICE_UNAVAILABLE

MESSAGES = "https://graph.microsoft.com/v1.0/me/messages"
MESSAGE_FIELDS = "id,conversationId,from,toRecipients,receivedDateTime,subject,body"


@dataclass(frozen=True)
class Message:
    message_id: str
    thread_id: str | None
    headers: dict[str, str]
    body: str


def thread_key(account_id: str, conversation_id: object) -> str | None:
    """Conversation ids are mailbox-local; absent conversations have no thread key."""
    if not isinstance(conversation_id, str) or not conversation_id:
        return None
    return f"microsoft:{account_id}:thread:{conversation_id}"


def _address(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    email = value.get("emailAddress")
    return str(email.get("address", "")) if isinstance(email, dict) else ""


def _message(value: object) -> Message:
    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
        raise MicrosoftError(SERVICE_UNAVAILABLE)
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
        params={"$search": f'\"{query}\"', "$top": min(maximum, 25), "$select": MESSAGE_FIELDS}, max_items=maximum,
    )
    return [_message(item) for item in pages]


async def fetch(account_id: str, message_id: str) -> Message:
    if not isinstance(message_id, str) or not message_id:
        raise MicrosoftError(REQUEST_REFUSED, never_sent=True)
    value = await client.graph_json("GET", f"{MESSAGES}/{quote(message_id, safe='')}", account_id=account_id)
    return _message(value)


async def send(
    account_id: str, recipients: list[str], subject: str, body: str, *, cc: list[str] | None = None,
    token: str | None = None,
) -> str:
    if not recipients or not all(isinstance(item, str) and item.strip() for item in recipients):
        raise MicrosoftError("request_refused", never_sent=True)
    if cc is not None and not all(isinstance(item, str) and item.strip() for item in cc):
        raise MicrosoftError("request_refused", never_sent=True)
    payload: dict[str, object] = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": item}} for item in recipients],
            "ccRecipients": [{"emailAddress": {"address": item}} for item in cc or []],
        },
    }
    request = {"account_id": account_id, "body": payload}
    if token is not None:
        request["token"] = token
    await client.graph_json("POST", "https://graph.microsoft.com/v1.0/me/sendMail", **request)
    # Graph returns 202 without an external message id; acceptance is the durable proof.
    return "accepted"
