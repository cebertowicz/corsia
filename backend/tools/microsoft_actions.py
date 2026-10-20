"""Microsoft actions, guarded by designated thread and account reach before Graph."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from connections.connected_accounts import ConnectedAccountError
from connections.microsoft import calendar, client, files, mail
from connections.microsoft.mail import thread_key
from connections.microsoft.provider import MICROSOFT
from connections.microsoft.errors import MicrosoftError
from connections.mail import email_screen
from connections.mail.email_document import DraftRefused, addresses, compose_document, parse_document, text_arg
from infra.db_async import run_in_db
from tools.action_delivery import deliver_claimed
from tools.action_drafts import DraftError
from tools.reviewed_drafts import DraftStorageRefused, open_draft, save_draft
from tools.action_gate import Approved, check_action
from tools.base import Tool
from tools.microsoft_account import _designated

log = logging.getLogger(__name__)
CLOSE_TAG = re.compile(r"</untrusted-email", re.I)
UNSCREENED = "This message hasn't been screened yet because the local model isn't available. Try again later."
ACCOUNT_PARAM = {"type": "string", "description": "The Microsoft account_id returned by microsoft_status."}


def _visible_account(selector: object, thread_id: object) -> dict[str, object]:
    from connections.connected_accounts import visible_account
    from infra.database import get_db

    if not isinstance(selector, str) or not selector or not isinstance(thread_id, str) or not thread_id:
        raise ConnectedAccountError("Microsoft account is unavailable to this thread")
    db = get_db()
    try:
        account = visible_account(selector, thread_id, db=db, provider=MICROSOFT.name)
    finally:
        db.close()
    if account is None:
        raise ConnectedAccountError("Microsoft account is unavailable to this thread")
    if account["state"] != "connected":
        raise ConnectedAccountError("Microsoft account needs sign-in")
    return account


async def _account(kwargs: dict[str, Any]) -> str:
    refusal = await _designated(kwargs)
    if refusal:
        raise ConnectedAccountError(refusal.removeprefix("Error: "))
    row = await run_in_db(_visible_account, kwargs.get("account_id"), kwargs.get("_thread_id"))
    return str(row["id"])


def _error(exc: Exception) -> str:
    if isinstance(exc, MicrosoftError):
        return f"Error: {exc.message}"
    if isinstance(exc, (ConnectedAccountError, DraftRefused, DraftStorageRefused, DraftError)):
        # Fixed refusal copy written in CORSIA; it tells the agent what to correct.
        return f"Error: {exc}"
    return "Error: Microsoft is unavailable. Try again later."


class MicrosoftMailSearchTool(Tool):
    self_describing_errors = True
    name = "microsoft_mail_search"
    description = "Search Microsoft mail in the required account_id; returns headers only."
    parameters = {"type": "object", "additionalProperties": False, "required": ["account_id", "query"], "properties": {"account_id": ACCOUNT_PARAM, "query": {"type": "string"}, "max": {"type": "integer", "minimum": 1, "maximum": 25}}}
    async def execute(self, **kwargs: Any) -> str:
        try:
            account = await _account(kwargs)
            query = kwargs.get("query")
            if not isinstance(query, str) or not query.strip():
                return "Error: query is required."
            messages = await mail.search(account, query, kwargs.get("max", 10))
            return json.dumps({"messages": [{"message_id": item.message_id, "thread_id": thread_key(account, item.thread_id), **item.headers} for item in messages]})
        except (MicrosoftError, ConnectedAccountError) as exc:
            return _error(exc)


class MicrosoftMailReadTool(Tool):
    self_describing_errors = True
    name = "microsoft_mail_read"
    description = "Read one Microsoft message after the local email screen passes it."
    parameters = {"type": "object", "additionalProperties": False, "required": ["account_id", "message_id"], "properties": {"account_id": ACCOUNT_PARAM, "message_id": {"type": "string"}}}
    async def execute(self, **kwargs: Any) -> str:
        try:
            account = await _account(kwargs)
            message = await mail.fetch(account, kwargs.get("message_id"))
            screen = await email_screen.screen_message(account, message.message_id, message.headers, message.body)
            if screen.flagged:
                return "Error: This message was flagged as a possible prompt injection. Its body is withheld."
            if not screen.clean:
                return f"Error: {UNSCREENED}"
            body = CLOSE_TAG.sub("&lt;/untrusted-email", message.body[:20_000])
            return json.dumps({"message_id": message.message_id, "thread_id": thread_key(account, message.thread_id), **message.headers, "body": f"<untrusted-email>\n{body}\n</untrusted-email>"})
        except (MicrosoftError, ConnectedAccountError) as exc:
            return _error(exc)


# Mutating mail and calendar actions are defined below with the required draft-and-confirmation gate.


class MicrosoftCalendarListTool(Tool):
    """List calendar events after validating the designated thread and account reach."""

    self_describing_errors = True
    name = "microsoft_calendar_list"
    description = "List Microsoft calendar events for the required account_id."
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "required": ["account_id", "start", "end"],
        "properties": {
            "account_id": ACCOUNT_PARAM,
            "start": {"type": "string"},
            "end": {"type": "string"},
        },
    }

    async def execute(self, **kwargs: Any) -> str:
        if not calendar.is_iso_datetime(kwargs.get("start")) or not calendar.is_iso_datetime(kwargs.get("end")):
            return "Error: start and end must be ISO-8601 datetimes."
        try:
            events = await calendar.list_events(
                await _account(kwargs), kwargs.get("start"), kwargs.get("end"),
            )
            return json.dumps({"events": events})
        except (MicrosoftError, ConnectedAccountError) as exc:
            return _error(exc)


class _FilesTool(Tool):
    self_describing_errors = True
    operation = ""
    async def execute(self, **kwargs: Any) -> str:
        try:
            account = await _account(kwargs)
            if self.operation == "list": result = await files.list_items(account, kwargs.get("parent_id", "root"))
            elif self.operation == "read": result = await files.read_item(account, kwargs.get("item_id"))
            else:
                name, content = kwargs.get("name"), kwargs.get("content")
                if not isinstance(name, str) or not name or "/" in name or not isinstance(content, str):
                    return "Error: name must not be empty or contain '/', and content must be text."
                result = await files.upload_item(account, name, content)
            return json.dumps({"files": result} if self.operation == "list" else result)
        except (MicrosoftError, ConnectedAccountError) as exc:
            return _error(exc)
class MicrosoftFilesListTool(_FilesTool):
    name = "microsoft_files_list"; description = "List Microsoft files for the required account_id."
    operation = "list"; parameters = {"type": "object", "additionalProperties": False, "required": ["account_id"], "properties": {"account_id": ACCOUNT_PARAM, "parent_id": {"type": "string"}}}
class MicrosoftFilesReadTool(_FilesTool):
    name = "microsoft_files_read"; description = "Read Microsoft file metadata for the required account_id."
    operation = "read"; parameters = {"type": "object", "additionalProperties": False, "required": ["account_id", "item_id"], "properties": {"account_id": ACCOUNT_PARAM, "item_id": {"type": "string"}}}
class MicrosoftFilesUploadTool(_FilesTool):
    name = "microsoft_files_upload"; description = "Upload a small text Microsoft file for the required account_id."
    operation = "upload"; parameters = {"type": "object", "additionalProperties": False, "required": ["account_id", "name", "content"], "properties": {"account_id": ACCOUNT_PARAM, "name": {"type": "string"}, "content": {"type": "string"}}}


# Reviewed-draft paths, writes, and account binding are owned by tools.reviewed_drafts.

class MicrosoftMailDraftTool(Tool):
    """Save a locally reviewed Microsoft mail draft before its separate send call."""
    self_describing_errors = True
    name = "microsoft_mail_draft"
    description = "Save a Microsoft email draft for the person to review before sending."
    parameters = {"type": "object", "additionalProperties": False, "required": ["account_id", "to", "subject", "body"], "properties": {"account_id": ACCOUNT_PARAM, "to": {"type": "array", "items": {"type": "string"}}, "cc": {"type": "array", "items": {"type": "string"}}, "subject": {"type": "string"}, "body": {"type": "string"}, "draft_id": {"type": "string"}}}

    async def execute(self, **kwargs: Any) -> str:
        try:
            account = await _account(kwargs)
            thread_id = kwargs.get("_thread_id")
            if not isinstance(thread_id, str) or not thread_id:
                return "Error: current thread identity is unavailable"
            to, cc = addresses(kwargs.get("to")), addresses(kwargs.get("cc"))
            subject, body = text_arg(kwargs.get("subject"), "subject is required", one_line=True), text_arg(kwargs.get("body"), "body is required")
            address = str((await run_in_db(_visible_account, account, thread_id))["address"])
            text = compose_document(address, to, cc, subject, body, "")
            saved = await save_draft(
                thread_id=thread_id, kind="email", extension=".md", prefix="email-drafts",
                account_id=account, target={"account_id": account, "account": address}, text=text,
                draft_id=kwargs.get("draft_id"), context=kwargs,
                description="Microsoft email draft; the person reviews it before anything is sent.",
            )
            row = await run_in_db(open_draft, thread_id, saved["draft_id"], "email")
            return json.dumps({"draft_id": saved["draft_id"], "revision": saved["revision"], "path": row["path"], "account_id": account, "next": "Show the person this draft and wait for their reply before calling microsoft_mail_send."})
        except (MicrosoftError, ConnectedAccountError, DraftRefused, DraftStorageRefused, DraftError) as exc:
            return _error(exc)


MAIL_STILL_SENDING = "Not sent: an earlier Microsoft send did not finish, so it may already have gone out. Check the person's Sent mail before saving a new draft."
MAIL_UNCONFIRMED = "Microsoft did not confirm the send, so the email may have gone out. Do not send this draft again: check the person's Sent mail first."
CALENDAR_STILL_SENDING = "Not sent: an earlier Microsoft calendar change did not finish, so it may already have happened. Check the person's calendar before saving a new draft."
CALENDAR_UNCONFIRMED = "Microsoft did not confirm the calendar change, so the event may exist. Do not send this draft again: check the person's calendar first."


class MicrosoftMailSendTool(Tool):
    """Send only the person-reviewed draft revision that action_gate approves."""
    self_describing_errors = True
    name = "microsoft_mail_send"
    description = "Send a reviewed Microsoft mail draft only after the person's later reply."
    parameters = {"type": "object", "additionalProperties": False, "required": ["account_id", "draft_id", "revision"], "properties": {"account_id": ACCOUNT_PARAM, "draft_id": {"type": "string"}, "revision": {"type": "string"}}}

    async def execute(self, **kwargs: Any) -> str:
        draft_id, revision = kwargs.get("draft_id"), kwargs.get("revision")
        if not isinstance(draft_id, str) or not isinstance(revision, str):
            return "Error: Pass the draft_id and revision microsoft_mail_draft returned."
        verdict = await check_action("email", draft_id, revision)
        if not isinstance(verdict, Approved):
            return f"Error: {verdict.text}"
        try:
            target = json.loads(verdict.draft.get("target_json") or "{}")
            if kwargs.get("account_id") != target.get("account_id"):
                return "Error: account_id does not match the draft."
            account = await _account(kwargs)
            row = await run_in_db(_visible_account, kwargs.get("account_id"), kwargs.get("_thread_id"))
            parsed = parse_document(verdict.data.decode("utf-8"))
            if parsed is None:
                return "Error: Not sent: the draft document is invalid."
            if parsed["from"] is not None and parsed["from"].lower() != str(row["address"]).lower():
                return "Error: Not sent: the From line does not match the Microsoft account this draft was written from."
            return await deliver_claimed(
                draft_id,
                acquire=lambda: client.access_token(account),
                provider_errors=(MicrosoftError,),
                send=lambda token: mail.send(account, parsed["to"], parsed["subject"], parsed["body"], cc=parsed["cc"], token=token),
                result_id=lambda result: result,
                success=lambda _result, _external_id: json.dumps({"sent": True, "account_id": account}),
                sign_in_error=lambda exc: exc.message if isinstance(exc, MicrosoftError) else "Microsoft sign-in is unavailable. Try again later.",
                request_error=lambda exc: exc.message if isinstance(exc, MicrosoftError) else "Microsoft could not send this request.",
                never_sent=lambda exc: isinstance(exc, MicrosoftError) and exc.never_sent,
                still_sending=MAIL_STILL_SENDING,
                claim_error="Not sent: could not check the draft. Try again.",
                unconfirmed=MAIL_UNCONFIRMED,
                label="microsoft_mail_send",
            )
        except (MicrosoftError, ConnectedAccountError, DraftError) as exc:
            return _error(exc)


class MicrosoftCalendarDraftTool(Tool):
    self_describing_errors = True
    name = "microsoft_calendar_draft"
    description = "Save a Microsoft calendar create or update draft for review."
    parameters = {"type": "object", "additionalProperties": False, "required": ["account_id", "operation", "event"], "properties": {"account_id": ACCOUNT_PARAM, "operation": {"type": "string", "enum": ["create", "update"]}, "event_id": {"type": "string"}, "event": {"type": "object"}}}
    async def execute(self, **kwargs: Any) -> str:
        try:
            account = await _account(kwargs)
            thread_id = kwargs.get("_thread_id")
            if not isinstance(thread_id, str) or not thread_id or not isinstance(kwargs.get("event"), dict): return "Error: event and current thread identity are required."
            operation = kwargs.get("operation")
            if operation == "create" and kwargs.get("event_id") is not None:
                return "Error: event_id is only valid for update."
            if operation == "update" and (not isinstance(kwargs.get("event_id"), str) or not kwargs["event_id"].strip()):
                return "Error: event_id is required for update."
            data = json.dumps({"operation": operation, "event_id": kwargs.get("event_id"), "event": kwargs["event"]}, separators=(",", ":"))
            saved = await save_draft(
                thread_id=thread_id, kind="invite", extension=".json", prefix="calendar-invites",
                account_id=account, target={"account_id": account, "operation": operation}, text=data,
                draft_id=kwargs.get("draft_id"), context=kwargs,
                description="Microsoft calendar draft; the person reviews it before anything is sent.",
            )
            row = await run_in_db(open_draft, thread_id, saved["draft_id"], "invite")
            return json.dumps({"draft_id": saved["draft_id"], "revision": saved["revision"], "path": row["path"], "account_id": account, "operation": operation, "next": f"Show the person this calendar change and wait for their reply. After approval, call microsoft_calendar_{operation} with this account_id, draft_id, and revision."})
        except (MicrosoftError, ConnectedAccountError, DraftError, DraftStorageRefused) as exc: return _error(exc)


class _GatedCalendarTool(Tool):
    self_describing_errors = True
    operation = ""
    async def execute(self, **kwargs: Any) -> str:
        draft_id, revision = kwargs.get("draft_id"), kwargs.get("revision")
        if not isinstance(draft_id, str) or not isinstance(revision, str): return "Error: Pass the account_id, draft_id, and revision microsoft_calendar_draft returned."
        verdict = await check_action("invite", draft_id, revision)
        if not isinstance(verdict, Approved): return f"Error: {verdict.text}"
        try:
            target, payload = json.loads(verdict.draft.get("target_json") or "{}"), json.loads(verdict.data)
            draft_operation = payload.get("operation") if isinstance(payload, dict) else None
            if draft_operation not in {"create", "update"} or not isinstance(payload.get("event"), dict):
                return "Error: This saved calendar draft is invalid or incomplete; nothing changed. Save a new draft with microsoft_calendar_draft, show the person, and wait for their reply."
            if draft_operation != self.operation:
                return f"Error: This draft is for {draft_operation}, not {self.operation}. Call microsoft_calendar_{draft_operation} with the same account_id, draft_id, and revision."
            if kwargs.get("account_id") != target.get("account_id"):
                return f"Error: account_id does not match this draft. Call microsoft_calendar_{self.operation} again with the account_id returned by microsoft_calendar_draft; to use another account, save a new draft with that account_id."
            account = await _account(kwargs)
            if self.operation == "update" and (not isinstance(payload.get("event_id"), str) or not payload["event_id"].strip()):
                return "Error: This saved calendar draft is invalid or incomplete; nothing changed. Save a new draft with microsoft_calendar_draft, show the person, and wait for their reply."
            async def send(token: str) -> dict[str, object]:
                if self.operation == "create":
                    return await calendar.create_event(account, payload["event"], token=token)
                return await calendar.update_event(account, payload["event_id"], payload["event"], token=token)
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
        except (MicrosoftError, ConnectedAccountError, DraftError, ValueError, KeyError, TypeError) as exc: return _error(exc)


class MicrosoftCalendarCreateTool(_GatedCalendarTool):
    name = "microsoft_calendar_create"; description = "Create a reviewed Microsoft calendar draft."; operation = "create"
    parameters = {"type": "object", "additionalProperties": False, "required": ["account_id", "draft_id", "revision"], "properties": {"account_id": ACCOUNT_PARAM, "draft_id": {"type": "string"}, "revision": {"type": "string"}}}
class MicrosoftCalendarUpdateTool(_GatedCalendarTool):
    name = "microsoft_calendar_update"; description = "Update a reviewed Microsoft calendar draft."; operation = "update"
    parameters = {"type": "object", "additionalProperties": False, "required": ["account_id", "draft_id", "revision"], "properties": {"account_id": ACCOUNT_PARAM, "draft_id": {"type": "string"}, "revision": {"type": "string"}}}
