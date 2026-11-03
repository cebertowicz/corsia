"""The check before every ``google_workspace`` call.

gws's ``--dry-run`` prints the one request a command would send (method, address, query
parameters, body) without sending it. The check reads only Google's method and address from
that preview, so gws's shortcuts and future commands are covered with nothing built for them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

from tools import gws_messages

RUN = "run"
TRASH_FILE = "trash_file"
DELETE_EVENT = "delete_event"
# Clearing or deleting a calendar: every event in it is saved first.
SAVE_CALENDAR = "save_calendar"

_DOCS_BATCH = re.compile(r"^/v1/documents/[^/]+:batchUpdate$")
_DRIVE_FILE = re.compile(r"^/drive/v3/files/([^/]+)$")
_CALENDAR_EVENT = re.compile(r"^/calendar/v3/calendars/([^/]+)/events/([^/]+)$")
_CALENDAR_CLEAR = re.compile(r"^/calendar/v3/calendars/([^/]+)/clear$")
_CALENDAR = re.compile(r"^/calendar/v3/calendars/([^/]+)$")
# Deletes that can simply be redone: a file's share, a calendar's sharing rule, a calendar in the list.
_REDOABLE_DELETES = (
    re.compile(r"^/drive/v3/files/[^/]+/permissions/[^/]+$"),
    re.compile(r"^/calendar/v3/calendars/[^/]+/acl/[^/]+$"),
    re.compile(r"^/calendar/v3/users/me/calendarList/[^/]+$"),
)
# A Docs request that removes or rewrites content, today's or a future one, which Google can
# refuse if someone edited since: its name starts with one of these.
_DESTRUCTIVE_DOCS_PREFIXES = ("delete", "replace")


@dataclass(frozen=True)
class Decision:
    """What to do with a previewed request: refuse it (``refusal``) or run it as ``kind``."""

    kind: str = RUN
    refusal: str | None = None
    file_id: str | None = None
    calendar_id: str | None = None
    event_id: str | None = None


def parse_preview(stdout: str) -> dict | None:
    """The one request gws printed, or None when the output is not exactly one request."""
    try:
        preview = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(preview, dict) or preview.get("dry_run") is not True:
        return None
    if not isinstance(preview.get("method"), str) or not isinstance(preview.get("url"), str):
        return None
    return preview


def _refuse(message: str) -> Decision:
    return Decision(refusal=message)


def _is_mail(host: str, path: str) -> bool:
    if host == "gmail.googleapis.com":
        return True
    return host == "www.googleapis.com" and path.startswith(("/gmail/", "/upload/gmail/"))


def _unguarded_docs_delete(preview: dict) -> bool:
    """True when a Docs batchUpdate deletes or replaces content without the revision the agent read."""
    body = preview.get("body")
    body = body if isinstance(body, dict) else {}
    requests = body.get("requests")
    destructive = isinstance(requests, list) and any(
        isinstance(request, dict) and any(name.startswith(_DESTRUCTIVE_DOCS_PREFIXES) for name in request)
        for request in requests
    )
    control = body.get("writeControl")
    revision = control.get("requiredRevisionId") if isinstance(control, dict) else None
    return destructive and not (isinstance(revision, str) and revision)


def _delete(host: str, path: str) -> Decision:
    """A delete runs only where it can be put back; any other delete, today's or a future one, is refused."""
    if host == "www.googleapis.com":
        if path == "/drive/v3/files/trash":
            return _refuse(gws_messages.TRASH)
        if found := _DRIVE_FILE.match(path):
            return Decision(TRASH_FILE, file_id=unquote(found.group(1)))
        if found := _CALENDAR_EVENT.match(path):
            return Decision(DELETE_EVENT, calendar_id=unquote(found.group(1)), event_id=unquote(found.group(2)))
        if found := _CALENDAR.match(path):
            return Decision(SAVE_CALENDAR, calendar_id=unquote(found.group(1)))
        if any(pattern.match(path) for pattern in _REDOABLE_DELETES):
            return Decision()
    return _refuse(gws_messages.CANT_UNDO)


def decide(preview: dict) -> Decision:
    """The decision for one previewed request, from Google's method and address alone."""
    method = preview["method"].upper()
    url = urlparse(preview["url"])
    host, path = (url.hostname or "").lower(), url.path
    if _is_mail(host, path):
        return _refuse(gws_messages.MAIL)
    if method == "DELETE":
        return _delete(host, path)
    if method == "POST" and host == "www.googleapis.com" and (found := _CALENDAR_CLEAR.match(path)):
        return Decision(SAVE_CALENDAR, calendar_id=unquote(found.group(1)))
    if method == "POST" and host == "docs.googleapis.com" and _DOCS_BATCH.match(path):
        if _unguarded_docs_delete(preview):
            return _refuse(gws_messages.DOCS_GUARD)
    return Decision()
