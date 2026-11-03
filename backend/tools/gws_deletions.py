"""The deletion log: what a Calendar event delete, calendar clear or calendar delete removes is saved first.

Google can't restore a deleted event or a cleared calendar. Before the delete runs, every event it
removes is written in full, as one JSON list, to
``<workspace>/google-deletions/<account>/<UTC timestamp>-<event|calendar>-<id>.json``; if anything
can't be saved, nothing is deleted. Only removals are logged: deletes, and updates that cancel an
event or set a series' recurrence.

Scale: one gws call per 2,500 events and one file per delete, so clearing a 10,000-event calendar
is four list calls and one file.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

from tools.gws_messages import NOT_SAVED
from tools.gws_run import UNSAFE_NAME, Completed

LOG_FOLDER = "google-deletions"
PAGE_SIZE = 2500

Run = Callable[[list[str]], Awaitable[Completed]]


class NotSaved(Exception):
    """An event couldn't be read or written, so the delete must not run; the text is safe to show."""


def log_dir(workspace: Path, address: str) -> Path:
    return workspace / LOG_FOLDER / UNSAFE_NAME.sub("_", address)


def failure_text(done: Completed) -> str:
    return (done.stdout + "\n" + done.stderr).strip()[:1500] or "gws failed."


def _write(folder: Path, name: str, events: list[dict]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = folder / f"{stamp}-{UNSAFE_NAME.sub('_', name)}.json"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(events, indent=2), encoding="utf-8")
    except OSError as error:
        raise NotSaved(NOT_SAVED.format(detail=type(error).__name__)) from None
    return path


def _json_object(done: Completed) -> dict:
    try:
        value = json.loads(done.stdout) if done.code == 0 else None
    except ValueError:
        value = None
    if not isinstance(value, dict):
        raise NotSaved(NOT_SAVED.format(detail=failure_text(done)))
    return value


async def _list_events(run: Run, params: dict) -> list[dict]:
    """Every event ``events.list`` returns for ``params``, across all pages."""
    events: list[dict] = []
    page_token = None
    while True:
        page_params = {**params, "maxResults": PAGE_SIZE}
        if page_token:
            page_params["pageToken"] = page_token
        page = _json_object(await run(["calendar", "events", "list", "--params", json.dumps(page_params)]))
        events.extend(event for event in page.get("items") or [] if isinstance(event, dict))
        page_token = page.get("nextPageToken")
        if not isinstance(page_token, str) or not page_token:
            return events


async def save_event(run: Run, folder: Path, calendar_id: str, event_id: str) -> Path:
    """Save an event before it is deleted; raises :class:`NotSaved` when it can't be.

    Deleting a recurring series also deletes its changed occurrences, which Google stores as
    separate events sharing the series' iCalUID, so the series and all of them are saved.
    """
    event = _json_object(await run([
        "calendar", "events", "get", "--params", json.dumps({"calendarId": calendar_id, "eventId": event_id}),
    ]))
    events = [event]
    if event.get("recurrence") and isinstance(event.get("iCalUID"), str):
        series = await _list_events(run, {"calendarId": calendar_id, "iCalUID": event["iCalUID"]})
        events += [item for item in series if item.get("id") != event.get("id")]
    return await asyncio.to_thread(_write, folder, f"event-{event_id}", events)


async def save_calendar(run: Run, folder: Path, calendar_id: str) -> Path:
    """Save every event of a calendar before it is cleared or deleted."""
    events = await _list_events(run, {"calendarId": calendar_id})
    return await asyncio.to_thread(_write, folder, f"calendar-{calendar_id}", events)
