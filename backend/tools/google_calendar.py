"""Calendar tools through the person's Google sign-in.

Invites follow the email draft/send lifecycle: the draft document is what the
person reviews, action_drafts owns its state, and the action gate approves the
exact revision before an event can be created.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from connections.google import client as google_client
from connections.google import oauth
from connections.google.calendar_document import attendee_emails, compose_document, one_line_text, parse_time
from connections.google.client import GoogleError, GoogleRequestFailed
from infra import config
from infra.db_async import run_in_db
from tools.action_drafts import (
    DraftError,
    claim_for_send,
    create_draft,
    mark_sent,
    record_draft,
    release_claim,
)
from tools.action_gate import Approved, Refusal, STALE_REVISION, check_action
from tools.base import Tool
from tools.google_account import _designated

log = logging.getLogger(__name__)

CALENDAR = "https://www.googleapis.com/calendar/v3"
DRAFT_DIR = "calendar-invites"
OFFSET_REQUIRED = "Give times with a UTC offset, e.g. 2026-10-15T09:00:00-07:00."
RANGE_TOO_LARGE = "Ask for at most 31 days at a time."
NO_OPEN_DRAFT = "Save the invite again with calendar_draft_invite and show the person."
BAD_DOCUMENT = (
    "Not sent: the invite draft document isn't in the draft format. Save it again with "
    "calendar_draft_invite, show the person, and wait for their reply."
)
STILL_SENDING = (
    "Not sent: an earlier send of this invite didn't finish, so it may already have gone out. "
    "Check the person's calendar for the event; if it isn't there, save a new invite with "
    "calendar_draft_invite and show the person."
)
AFTER_DRAFT = (
    "Show the person the invite draft (link its path), end your turn, and call "
    "calendar_send_invite only when their reply says to send it; if they ask for changes, "
    "save again with calendar_draft_invite and show them the new version."
)
UNCONFIRMED = (
    "Google didn't confirm the invite send, so the event may exist. Don't send this invite "
    "again: check the person's calendar first."
)
CUT_NOTE = "The list was cut at 250 events; narrow the range."


def _workspace_root() -> Path:
    return config.WORKSPACE.resolve()


def _new_draft_path(thread_id: str) -> Path:
    from core.workspace_layout import (
        resolve_project_root,
        resolve_thread_working_root,
        resolve_workspace_root,
    )
    from infra.database import get_db

    workspace = resolve_workspace_root(config.WORKSPACE)
    db = get_db()
    try:
        row = db.execute("SELECT project_id FROM threads WHERE id = ?", (thread_id,)).fetchone()
        project_id = row["project_id"] if row else None
        scope = resolve_project_root(workspace, project_id, db=db) if project_id else workspace
    finally:
        db.close()
    if scope is None:
        raise ValueError("The project's folder is unavailable, so the invite was not saved.")
    return resolve_thread_working_root(scope, thread_id) / DRAFT_DIR / f"invite-{uuid.uuid4().hex[:12]}.md"


def _relative(path: Path) -> str:
    relative = os.path.relpath(path.resolve(), _workspace_root())
    if relative.startswith(".."):
        raise ValueError("The invite's folder is outside the workspace, so it was not saved.")
    return relative


def _open_draft(thread_id: str, draft_id: str) -> dict | None:
    from infra.database import get_db

    db = get_db()
    try:
        row = db.execute(
            "SELECT id, path, state, target_json FROM action_drafts"
            " WHERE id=? AND thread_id=? AND kind='invite'",
            (draft_id, thread_id),
        ).fetchone()
    finally:
        db.close()
    return dict(row) if row else None


def _read_document(relative: str) -> bytes | None:
    from tools.action_gate import _read_bytes

    return _read_bytes(relative)



def _event_url(calendar_id: str) -> str:
    return f"{CALENDAR}/calendars/{quote(calendar_id, safe='')}/events"


def _event_time(event: dict, key: str) -> str | None:
    value = event.get(key)
    if not isinstance(value, dict):
        return None
    candidate = value.get("dateTime", value.get("date"))
    return candidate if isinstance(candidate, str) else None


class CalendarListTool(Tool):
    self_describing_errors = True

    @property
    def name(self) -> str:
        return "calendar_list"

    @property
    def description(self) -> str:
        return "List the person's Google Calendar events in a time range of up to 31 days"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object", "additionalProperties": False,
            "properties": {
                "time_min": {"type": "string", "description": "RFC3339 time with a UTC offset."},
                "time_max": {"type": "string", "description": "RFC3339 time with a UTC offset."},
                "calendar_id": {"type": "string", "description": "Google calendar id; defaults to primary."},
            },
            "required": ["time_min", "time_max"],
        }

    async def execute(self, **kwargs: Any) -> str:
        if refusal := await _designated(kwargs):
            return refusal
        try:
            time_min, start = parse_time(kwargs.get("time_min"))
            time_max, end = parse_time(kwargs.get("time_max"))
            if end <= start:
                return "Error: time_max must be after time_min."
            if end - start > timedelta(days=31):
                return f"Error: {RANGE_TOO_LARGE}"
            calendar_id = kwargs.get("calendar_id", "primary")
            if not isinstance(calendar_id, str) or not calendar_id:
                return "Error: calendar_id must be a calendar id."
            response = await google_client.get_json(_event_url(calendar_id), params={
                "timeMin": time_min, "timeMax": time_max, "maxResults": 250,
                "singleEvents": "true", "orderBy": "startTime",
            })
        except ValueError as exc:
            return f"Error: {exc}"
        except GoogleError as exc:
            return f"Error: {exc}"
        except Exception:
            log.exception("calendar_list failed")
            return "Error: Couldn't read the calendar. Try again later."
        items = response.get("items") if isinstance(response, dict) else None
        events = []
        for event in items if isinstance(items, list) else []:
            if not isinstance(event, dict):
                continue
            attendees = event.get("attendees")
            events.append({
                "id": event.get("id"), "start": _event_time(event, "start"),
                "end": _event_time(event, "end"), "title": event.get("summary", ""),
                "attendees": [item["email"] for item in attendees if isinstance(item, dict) and isinstance(item.get("email"), str)] if isinstance(attendees, list) else [],
                "free/busy": "free" if event.get("transparency") == "transparent" else "busy",
            })
        result: dict[str, Any] = {"events": events}
        if isinstance(response, dict) and response.get("nextPageToken"):
            result["note"] = CUT_NOTE
        return json.dumps(result)


class CalendarDraftInviteTool(Tool):
    self_describing_errors = True

    @property
    def name(self) -> str:
        return "calendar_draft_invite"

    @property
    def description(self) -> str:
        return "Save a calendar invite as a draft document for the person to review; nothing is sent"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object", "additionalProperties": False,
            "properties": {
                "draft_id": {"type": "string", "description": "An open invite draft to replace; omit for a new one."},
                "title": {"type": "string", "description": "One-line event title."},
                "start": {"type": "string", "description": "RFC3339 start with a UTC offset."},
                "end": {"type": "string", "description": "RFC3339 end with a UTC offset."},
                "attendees": {"type": "array", "items": {"type": "string"}, "description": "Attendee email addresses."},
                "description": {"type": "string", "description": "Optional event description."},
            },
            "required": ["title", "start", "end", "attendees"],
        }

    async def execute(self, **kwargs: Any) -> str:
        if refusal := await _designated(kwargs):
            return refusal
        try:
            return await self._save(kwargs["_thread_id"], kwargs)
        except ValueError as exc:
            return f"Error: {exc}"
        except GoogleError as exc:
            return f"Error: The invite was not saved. {exc}"
        except DraftError as exc:
            return f"Error: {exc}"
        except Exception:
            log.exception("calendar_draft_invite failed")
            return "Error: Couldn't save the invite draft."

    async def _save(self, thread_id: str, kwargs: dict[str, Any]) -> str:
        from tools.filesystem import WriteFileTool

        title = one_line_text(kwargs.get("title"), "title")
        start, parsed_start = parse_time(kwargs.get("start"))
        end, parsed_end = parse_time(kwargs.get("end"))
        if parsed_end <= parsed_start:
            raise ValueError("The invite end must be after its start.")
        attendees = attendee_emails(kwargs.get("attendees"))
        description = kwargs.get("description", "")
        if not isinstance(description, str):
            raise ValueError("The invite description must be text.")
        draft_id = kwargs.get("draft_id")
        existing = None
        if draft_id is not None:
            existing = await run_in_db(_open_draft, thread_id, str(draft_id))
            if existing is None or existing["state"] != "open":
                raise ValueError(NO_OPEN_DRAFT)
        account = await run_in_db(oauth.account_status)
        email = account.get("email")
        if not isinstance(email, str) or not email:
            raise GoogleError(google_client.NOT_CONNECTED)
        text = compose_document(email, title, start, end, attendees, description)
        path = _workspace_root() / existing["path"] if existing is not None else await run_in_db(_new_draft_path, thread_id)
        relative = _relative(path)
        context = {key: value for key, value in kwargs.items() if key.startswith("_")}
        written = await WriteFileTool(workspace=config.WORKSPACE).execute(
            path=str(path), content=text,
            description="Calendar invite draft; the person reviews it before anything is sent.",
            **context,
        )
        if isinstance(written, str) and written.startswith("Error"):
            return written
        data = await run_in_db(_read_document, relative)
        if data is None:
            raise ValueError("The invite draft was written but couldn't be read back, so it is not recorded.")
        target = {"title": title, "start": start, "end": end, "attendees": attendees, "description": description}
        saved = await run_in_db(record_draft, existing["id"], data, target) if existing else await run_in_db(create_draft, thread_id, "invite", relative, data, target)
        return json.dumps({
            "draft_id": saved["draft_id"], "revision": saved["revision"], "path": relative,
            "from": email, "next": AFTER_DRAFT,
        })


class CalendarSendInviteTool(Tool):
    self_describing_errors = True

    @property
    def name(self) -> str:
        return "calendar_send_invite"

    @property
    def description(self) -> str:
        return "Send a saved invite draft after the person says to send it; attendees get emails"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object", "additionalProperties": False,
            "properties": {
                "draft_id": {"type": "string", "description": "The draft_id calendar_draft_invite returned."},
                "revision": {"type": "string", "description": "The revision calendar_draft_invite returned."},
            },
            "required": ["draft_id", "revision"],
        }

    async def execute(self, **kwargs: Any) -> str:
        if refusal := await _designated(kwargs):
            return refusal
        draft_id, revision = kwargs.get("draft_id"), kwargs.get("revision")
        if not isinstance(draft_id, str) or not draft_id or not isinstance(revision, str) or not revision:
            return "Error: Pass the draft_id and revision calendar_draft_invite returned."
        try:
            prepared = await self._prepare(kwargs["_thread_id"], draft_id, revision)
        except Exception:
            log.exception("calendar_send_invite could not check the invite")
            return "Error: Not sent: couldn't check the invite draft. Try again."
        if isinstance(prepared, str):
            return prepared
        return await self._deliver(draft_id, prepared)

    async def _prepare(self, thread_id: str, draft_id: str, revision: str) -> dict | str:
        row = await run_in_db(_open_draft, thread_id, draft_id)
        if row is None:
            return f"Error: {NO_OPEN_DRAFT}"
        if row["state"] == "sending":
            return f"Error: {STILL_SENDING}"
        verdict = await check_action("invite", draft_id, revision)
        if isinstance(verdict, Refusal):
            if verdict.text == STALE_REVISION:
                return f"Error: {NO_OPEN_DRAFT}"
            return f"Error: {verdict.text}"
        try:
            if not verdict.data.decode("utf-8").startswith("From: "):
                return f"Error: {BAD_DOCUMENT}"
            target = json.loads(verdict.draft.get("target_json") or "{}")
            if not isinstance(target, dict):
                return f"Error: {BAD_DOCUMENT}"
            title = one_line_text(target.get("title"), "title")
            start, parsed_start = parse_time(target.get("start"))
            end, parsed_end = parse_time(target.get("end"))
            if parsed_end <= parsed_start:
                return f"Error: {BAD_DOCUMENT}"
            attendees = attendee_emails(target.get("attendees"))
            description = target.get("description", "")
            if not isinstance(description, str):
                return f"Error: {BAD_DOCUMENT}"
        except (UnicodeDecodeError, ValueError):
            return f"Error: {BAD_DOCUMENT}"
        return {
            "summary": title, "start": {"dateTime": start}, "end": {"dateTime": end},
            "attendees": [{"email": email} for email in attendees], "description": description,
        }

    async def _deliver(self, draft_id: str, body: dict) -> str:
        try:
            await run_in_db(claim_for_send, draft_id)
        except DraftError:
            return f"Error: {STILL_SENDING}"
        except Exception:
            log.exception("calendar_send_invite could not claim the invite")
            return "Error: Not sent: couldn't check the invite draft. Try again."
        try:
            bearer = await google_client.access_token()
        except GoogleError as exc:
            await self._release(draft_id)
            return f"Error: Not sent: {exc}"
        except Exception:
            log.exception("calendar_send_invite could not get a Google sign-in")
            await self._release(draft_id)
            return "Error: Not sent: couldn't reach Google. Try again later."
        try:
            sent = await google_client.request_json(
                "POST", _event_url("primary"), body=body, params={"sendUpdates": "all"}, token=bearer,
            )
        except google_client.GoogleSignInRejected as exc:
            await self._release(draft_id)
            return f"Error: Not sent: {exc}"
        except GoogleRequestFailed as exc:
            if exc.never_sent:
                await self._release(draft_id)
                return f"Error: Not sent: {exc}"
            return f"Error: {UNCONFIRMED}"
        except Exception:
            log.exception("calendar_send_invite outcome unknown")
            return f"Error: {UNCONFIRMED}"
        event_id = sent.get("id") if isinstance(sent, dict) else None
        if not isinstance(event_id, str) or not event_id:
            return f"Error: {UNCONFIRMED}"
        try:
            await run_in_db(mark_sent, draft_id, event_id)
        except Exception:
            log.exception("calendar_send_invite: Google accepted the event but the draft could not be marked sent")
        return json.dumps({"event_id": event_id, "html_link": sent.get("htmlLink")})

    @staticmethod
    async def _release(draft_id: str) -> None:
        try:
            await run_in_db(release_claim, draft_id)
        except Exception:
            log.exception("calendar_send_invite: the claim could not be released")
