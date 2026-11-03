"""The deletion log: what a Calendar event delete, calendar clear or calendar delete removes is saved first.

Google can't restore a deleted event or a cleared calendar. Each event is written in full to
``<workspace>/google-deletions/<account>/<UTC timestamp>-event-<id>.json`` before the delete
runs; if anything can't be saved, nothing is deleted. Only deletes are logged.
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


def _write(folder: Path, event_id: str, text: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = folder / f"{stamp}-event-{UNSAFE_NAME.sub('_', event_id)}.json"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as error:
        raise NotSaved(NOT_SAVED.format(detail=type(error).__name__)) from None
    return path


async def save_event(run: Run, folder: Path, calendar_id: str, event_id: str) -> Path:
    """Read one event in full and save it; raises :class:`NotSaved` when it can't be."""
    params = json.dumps({"calendarId": calendar_id, "eventId": event_id})
    done = await run(["calendar", "events", "get", "--params", params])
    if done.code != 0:
        raise NotSaved(NOT_SAVED.format(detail=failure_text(done)))
    return await asyncio.to_thread(_write, folder, event_id, done.stdout)


async def save_calendar(run: Run, folder: Path, calendar_id: str) -> list[Path]:
    """Save every event of a calendar (all pages) before it is cleared."""
    saved: list[Path] = []
    token: str | None = None
    while True:
        params = {"calendarId": calendar_id, "maxResults": PAGE_SIZE}
        if token:
            params["pageToken"] = token
        done = await run(["calendar", "events", "list", "--params", json.dumps(params)])
        try:
            page = json.loads(done.stdout) if done.code == 0 else None
        except ValueError:
            page = None
        if not isinstance(page, dict):
            raise NotSaved(NOT_SAVED.format(detail=failure_text(done)))
        for event in page.get("items") or []:
            if isinstance(event, dict) and isinstance(event.get("id"), str):
                saved.append(await asyncio.to_thread(_write, folder, event["id"], json.dumps(event, indent=2)))
        token = page.get("nextPageToken")
        if not isinstance(token, str) or not token:
            return saved
