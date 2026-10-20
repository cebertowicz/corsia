"""Microsoft Graph calendar adapter."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote

from connections.microsoft import client
from connections.microsoft.errors import MicrosoftError, PROVIDER_UNAVAILABLE

EVENTS = "https://graph.microsoft.com/v1.0/me/calendar/events"
CALENDAR_VIEW = "https://graph.microsoft.com/v1.0/me/calendarView"
LIST_LIMIT = 25


def _event(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
        raise MicrosoftError(PROVIDER_UNAVAILABLE)
    return {key: value.get(key) for key in ("id", "subject", "start", "end", "webLink")}


def is_iso_datetime(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


async def list_events(account_id: str, start: str, end: str, maximum: int = LIST_LIMIT) -> list[dict[str, object]]:
    if not is_iso_datetime(start) or not is_iso_datetime(end):
        raise MicrosoftError("request_refused", never_sent=True)
    limit = max(1, min(maximum, LIST_LIMIT))
    values = await client.graph_pages(
        CALENDAR_VIEW,
        account_id=account_id,
        params={
            "startDateTime": start,
            "endDateTime": end,
            "$select": "id,subject,start,end,webLink",
            "$top": limit,
        },
        max_items=limit,
    )
    return [_event(value) for value in values]


async def create_event(
    account_id: str, event: dict[str, object], *, token: str | None = None,
) -> dict[str, object]:
    request = {"account_id": account_id, "body": event}
    if token is not None:
        request["token"] = token
    return _event(await client.graph_json("POST", EVENTS, **request))


async def update_event(
    account_id: str, event_id: str, event: dict[str, object], *, token: str | None = None,
) -> dict[str, object]:
    if not isinstance(event_id, str) or not event_id.strip():
        raise MicrosoftError("request_refused", never_sent=True)
    request = {"account_id": account_id, "body": event}
    if token is not None:
        request["token"] = token
    return _event(await client.graph_json("PATCH", f"{EVENTS}/{quote(event_id, safe='')}", **request))
