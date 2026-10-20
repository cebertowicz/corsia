"""Microsoft calendar invites: readable drafts the person reviews, sent only from their recorded fields."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from connections.calendar_document import attendee_emails, compose_document, one_line_text, parse_time
from connections.connected_accounts import ConnectedAccountError
from connections.microsoft import calendar, client
from connections.microsoft.errors import MicrosoftError
from infra.db_async import run_in_db
from tools.action_delivery import deliver_claimed
from tools.action_drafts import DraftError
from tools.action_gate import Approved, check_action
from tools.base import Tool
from tools.microsoft_actions import ACCOUNT_PARAM, _account, _error, _visible_account
from tools.reviewed_drafts import DraftStorageRefused, open_draft, save_draft


CALENDAR_STILL_SENDING = "Not sent: an earlier Microsoft calendar change did not finish, so it may already have happened. Check the person's calendar before saving a new draft."
CALENDAR_UNCONFIRMED = "Microsoft did not confirm the calendar change, so the event may exist. Do not send this draft again: check the person's calendar first."


class MicrosoftCalendarDraftTool(Tool):
    self_describing_errors = True
    name = "microsoft_calendar_draft"
    description = (
        "Save a readable Microsoft calendar invite from the account named by account_id for the person to review; "
        "nothing is sent, and attendees get emails only after the reviewed draft is sent."
    )
    parameters = {
        "type": "object", "additionalProperties": False,
        "required": ["account_id", "operation", "title", "start", "end", "attendees"],
        "properties": {
            "account_id": ACCOUNT_PARAM,
            "operation": {"type": "string", "enum": ["create", "update"]},
            "event_id": {"type": "string", "description": "Event id to update; required only for update."},
            "draft_id": {"type": "string", "description": "An open invite draft to replace; omit for a new one."},
            "title": {"type": "string", "description": "One-line event title."},
            "start": {"type": "string", "description": "RFC3339 start with a UTC offset."},
            "end": {"type": "string", "description": "RFC3339 end with a UTC offset."},
            "attendees": {"type": "array", "items": {"type": "string"}, "description": "Attendee email addresses."},
            "description": {"type": "string", "description": "Optional event description."},
        },
    }

    async def execute(self, **kwargs: Any) -> str:
        try:
            account = await _account(kwargs)
            return await self._save(str(kwargs["_thread_id"]), account, kwargs)
        except ValueError as exc:
            return f"Error: {exc}"
        except (MicrosoftError, ConnectedAccountError, DraftError, DraftStorageRefused, KeyError) as exc:
            return _error(exc)

    async def _save(self, thread_id: str, account: str, kwargs: dict[str, Any]) -> str:
        operation = kwargs.get("operation")
        if operation not in {"create", "update"}:
            raise ValueError("operation must be create or update.")
        event_id = kwargs.get("event_id")
        if operation == "create" and event_id is not None:
            raise ValueError("event_id is only valid for update.")
        if operation == "update" and (not isinstance(event_id, str) or not event_id.strip()):
            raise ValueError("event_id is required for update.")
        title = one_line_text(kwargs.get("title"), "title")
        start, parsed_start = parse_time(kwargs.get("start"))
        end, parsed_end = parse_time(kwargs.get("end"))
        if parsed_end <= parsed_start:
            raise ValueError("The invite end must be after its start.")
        attendees = attendee_emails(kwargs.get("attendees"))
        description = kwargs.get("description", "")
        if not isinstance(description, str):
            raise ValueError("The invite description must be text.")
        row = await run_in_db(_visible_account, account, thread_id)
        address = str(row["address"])
        text = compose_document(address, title, start, end, attendees, description)
        target = {
            "account": address, "account_id": account, "operation": operation, "event_id": event_id,
            "title": title, "start": start, "end": end, "attendees": attendees, "description": description,
        }
        saved = await save_draft(
            thread_id=thread_id, kind="invite", extension=".md", prefix="calendar-invites",
            account_id=account, target=target, text=text, draft_id=kwargs.get("draft_id"), context=kwargs,
            description="Microsoft calendar invite draft; the person reviews it before anything is sent.",
        )
        row = await run_in_db(open_draft, thread_id, saved["draft_id"], "invite")
        return json.dumps({
            "draft_id": saved["draft_id"], "revision": saved["revision"], "path": row["path"],
            "account_id": account, "operation": operation,
            "next": f"Show the person this calendar invite and wait for their reply. After approval, call microsoft_calendar_{operation} with this account_id, draft_id, and revision.",
        })


def _graph_utc(value: datetime) -> str:
    # Graph's dateTimeTimeZone takes a local time without an offset; the zone rides in timeZone.
    return value.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


BAD_CALENDAR_DRAFT = (
    "This saved calendar draft is invalid or incomplete; nothing changed. Save a new draft with "
    "microsoft_calendar_draft, show the person, and wait for their reply."
)


class _GatedCalendarTool(Tool):
    self_describing_errors = True
    operation = ""

    async def execute(self, **kwargs: Any) -> str:
        draft_id, revision = kwargs.get("draft_id"), kwargs.get("revision")
        if not isinstance(draft_id, str) or not isinstance(revision, str):
            return "Error: Pass the account_id, draft_id, and revision microsoft_calendar_draft returned."
        verdict = await check_action("invite", draft_id, revision)
        if not isinstance(verdict, Approved):
            return f"Error: {verdict.text}"
        try:
            prepared = self._prepare(verdict)
            if isinstance(prepared, str):
                return f"Error: {prepared}"
            draft_operation, event_id, body = prepared
            if draft_operation != self.operation:
                return (
                    f"Error: This draft is for {draft_operation}, not {self.operation}. Call "
                    f"microsoft_calendar_{draft_operation} with the same account_id, draft_id, and revision."
                )
            target = json.loads(verdict.draft.get("target_json") or "{}")
            if not isinstance(target, dict) or kwargs.get("account_id") != target.get("account_id"):
                return (
                    f"Error: account_id does not match this draft. Call microsoft_calendar_{self.operation} "
                    "again with the account_id returned by microsoft_calendar_draft; to use another account, "
                    "save a new draft with that account_id."
                )
            account = await _account(kwargs)
            row = await run_in_db(_visible_account, account, kwargs.get("_thread_id"))
            if str(row["address"]).lower() != str(target.get("account")).lower():
                return "Error: Not sent: the From line does not match the Microsoft account this draft was written from."

            async def send(token: str) -> dict[str, object]:
                if self.operation == "create":
                    return await calendar.create_event(account, body, token=token)
                assert event_id is not None
                return await calendar.update_event(account, event_id, body, token=token)

            return await deliver_claimed(
                draft_id,
                acquire=lambda: client.access_token(account),
                provider_errors=(MicrosoftError,),
                send=send,
                result_id=lambda result: result.get("id") if isinstance(result, dict) else None,
                success=lambda result, _external_id: json.dumps(result),
                sign_in_error=lambda exc: exc.message if isinstance(exc, MicrosoftError) else "Microsoft sign-in is unavailable. Try again later.",
                request_error=lambda exc: exc.message if isinstance(exc, MicrosoftError) else "Microsoft could not send this request.",
                never_sent=lambda exc: isinstance(exc, MicrosoftError) and exc.never_sent,
                still_sending=CALENDAR_STILL_SENDING,
                claim_error="Not sent: could not check the calendar draft. Try again.",
                unconfirmed=CALENDAR_UNCONFIRMED,
                label=f"microsoft_calendar_{self.operation}",
            )
        except (MicrosoftError, ConnectedAccountError, DraftError, ValueError, KeyError, TypeError) as exc:
            return _error(exc)

    def _prepare(self, verdict: Approved) -> tuple[str, str | None, dict[str, object]] | str:
        try:
            if not verdict.data.decode("utf-8").startswith("From: "):
                return BAD_CALENDAR_DRAFT
            target = json.loads(verdict.draft.get("target_json") or "{}")
            if not isinstance(target, dict):
                return BAD_CALENDAR_DRAFT
            operation = target.get("operation")
            if operation not in {"create", "update"}:
                return BAD_CALENDAR_DRAFT
            title = one_line_text(target.get("title"), "title")
            start, parsed_start = parse_time(target.get("start"))
            end, parsed_end = parse_time(target.get("end"))
            if parsed_end <= parsed_start:
                return BAD_CALENDAR_DRAFT
            attendees = attendee_emails(target.get("attendees"))
            description = target.get("description", "")
            if not isinstance(description, str) or not isinstance(target.get("account"), str):
                return BAD_CALENDAR_DRAFT
            event_id = target.get("event_id")
            if operation == "update" and (not isinstance(event_id, str) or not event_id.strip()):
                return BAD_CALENDAR_DRAFT
        except (UnicodeDecodeError, ValueError, TypeError):
            return BAD_CALENDAR_DRAFT
        body: dict[str, object] = {
            "subject": title,
            "start": {"dateTime": _graph_utc(parsed_start), "timeZone": "UTC"},
            "end": {"dateTime": _graph_utc(parsed_end), "timeZone": "UTC"},
            "attendees": [{"emailAddress": {"address": email}, "type": "required"} for email in attendees],
            "body": {"contentType": "text", "content": description},
        }
        return operation, event_id if isinstance(event_id, str) else None, body


class MicrosoftCalendarCreateTool(_GatedCalendarTool):
    name = "microsoft_calendar_create"; description = "Create a reviewed Microsoft calendar draft."; operation = "create"
    parameters = {"type": "object", "additionalProperties": False, "required": ["account_id", "draft_id", "revision"], "properties": {"account_id": ACCOUNT_PARAM, "draft_id": {"type": "string"}, "revision": {"type": "string"}}}
class MicrosoftCalendarUpdateTool(_GatedCalendarTool):
    name = "microsoft_calendar_update"; description = "Update a reviewed Microsoft calendar draft."; operation = "update"
    parameters = {"type": "object", "additionalProperties": False, "required": ["account_id", "draft_id", "revision"], "properties": {"account_id": ACCOUNT_PARAM, "draft_id": {"type": "string"}, "revision": {"type": "string"}}}
