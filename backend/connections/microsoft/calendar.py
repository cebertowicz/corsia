"""Microsoft Graph calendar adapter."""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from connections.microsoft import client
from connections.microsoft.errors import MicrosoftError, PROVIDER_UNAVAILABLE

EVENTS = "https://graph.microsoft.com/v1.0/me/calendar/events"
LIST_LIMIT = 25


def _event(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
        raise MicrosoftError(PROVIDER_UNAVAILABLE)
    return {key: value.get(key) for key in ("id", "subject", "start", "end", "webLink")}


async def list_events(account_id: str, start: str, end: str, maximum: int = LIST_LIMIT) -> list[dict[str, object]]:
    limit = max(1, min(maximum, LIST_LIMIT))
    values = await client.graph_pages(EVENTS, account_id=account_id, params={"$filter": f"start/dateTime ge '{start}' and end/dateTime le '{end}'", "$select": "id,subject,start,end,webLink", "$top": limit}, max_items=limit)
    return [_event(value) for value in values]


async def create_event(account_id: str, event: dict[str, object]) -> dict[str, object]:
    return _event(await client.graph_json("POST", EVENTS, account_id=account_id, body=event))


async def update_event(account_id: str, event_id: str, event: dict[str, object]) -> dict[str, object]:
    if not isinstance(event_id, str) or not event_id:
        raise MicrosoftError(PROVIDER_UNAVAILABLE)
    return _event(await client.graph_json("PATCH", f"{EVENTS}/{quote(event_id, safe='')}", account_id=account_id, body=event))
