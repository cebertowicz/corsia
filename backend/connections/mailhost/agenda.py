"""List one mail-host account's calendar events in a window, in the fields Google's list returns."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from connections.account_errors import AccountError
from connections.mailhost import caldav, events
from connections.mailhost.read import credentials
from infra.db_async import run_in_db

MAIL_ONLY = "This account's host didn't offer a calendar to CORSIA, so only its email can be used."
UNAVAILABLE = "This account's calendar isn't available right now. Try again later."


async def list_events(account_id: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Every event the host's calendars hold between ``start`` and ``end``, recurring ones expanded."""
    endpoints, _username, password, _check = await run_in_db(credentials, account_id)
    if not endpoints.calendars:
        raise AccountError(MAIL_ONLY)
    try:
        texts = await caldav.fetch_calendar_data(endpoints, password, start, end)
    except caldav.CalendarUnavailable:
        raise AccountError(UNAVAILABLE) from None
    return events.summaries(texts, (start, end))
