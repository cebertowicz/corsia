"""Microsoft actions, guarded by designated thread and account reach before Graph."""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any

from connections.connected_accounts import ConnectedAccountError
from connections.microsoft import calendar, files, mail
from connections.microsoft.accounts import ACCOUNT_PARAM, resolve_for_thread, thread_key
from connections.microsoft.errors import MicrosoftError
from connections.google import email_screen
from connections.google.email_document import DraftRefused, addresses, compose_document, parse_document, text_arg
from infra import config
from infra.db_async import run_in_db
from tools.action_drafts import DraftError, claim_for_send, create_draft, mark_sent, record_draft, release_claim
from tools.action_gate import Approved, check_action
from tools.base import Tool
from tools.microsoft_account import _designated

log = logging.getLogger(__name__)
CLOSE_TAG = re.compile(r"</untrusted-email", re.I)
UNSCREENED = "This message hasn't been screened yet because the local model isn't available. Try again later."


async def _account(kwargs: dict[str, Any]) -> str:
    refusal = await _designated(kwargs)
    if refusal:
        raise ConnectedAccountError(refusal.removeprefix("Error: "))
    row = await run_in_db(resolve_for_thread, kwargs.get("account_id"), kwargs.get("_thread_id"))
    return str(row["id"])


def _error(exc: Exception) -> str:
    return f"Error: {getattr(exc, 'cause', str(exc))}" if str(exc) else "Error: provider_unavailable"


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
            else: result = await files.upload_item(account, kwargs.get("name"), kwargs.get("content"))
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


def _draft_path(thread_id: str) -> Path:
    return config.WORKSPACE / ".threads" / thread_id / "email-drafts" / f"microsoft-{uuid.uuid4().hex[:12]}.md"


def _draft_row(thread_id: str, draft_id: str) -> dict[str, object] | None:
    from infra.database import get_db
    db = get_db()
    try:
        row = db.execute("SELECT id,path,state,target_json FROM action_drafts WHERE id=? AND thread_id=? AND kind='email'", (draft_id, thread_id)).fetchone()
        return dict(row) if row else None
    finally:
        db.close()


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
            text = compose_document(str((await run_in_db(resolve_for_thread, account, thread_id))["address"]), to, cc, subject, body, "")
            old = await run_in_db(_draft_row, thread_id, str(kwargs["draft_id"])) if kwargs.get("draft_id") else None
            if old is not None and old.get("state") != "open":
                return "Error: There is no open draft with that id in this conversation."
            path = config.WORKSPACE / str(old["path"]) if old else _draft_path(thread_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            relative = os.path.relpath(path, config.WORKSPACE)
            data = text.encode("utf-8")
            target = {"account_id": account}
            saved = await run_in_db(record_draft, str(old["id"]), data, target) if old else await run_in_db(create_draft, thread_id, "email", relative, data, target)
            return json.dumps({"draft_id": saved["draft_id"], "revision": saved["revision"], "path": relative, "account_id": account, "next": "Show the person this draft and wait for their reply before calling microsoft_mail_send."})
        except (MicrosoftError, ConnectedAccountError, DraftRefused, DraftError) as exc:
            return _error(exc)


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
            parsed = parse_document(verdict.data.decode("utf-8"))
            if parsed is None:
                return "Error: Not sent: the draft document is invalid."
            await run_in_db(claim_for_send, draft_id)
            await mail.send(account, parsed["to"], parsed["subject"], parsed["body"])
            await run_in_db(mark_sent, draft_id, "microsoft")
            return json.dumps({"sent": True, "account_id": account})
        except (MicrosoftError, ConnectedAccountError, DraftError) as exc:
            try:
                await run_in_db(release_claim, draft_id)
            except Exception as release_error:
                log.warning("could not release Microsoft mail draft claim: %s", type(release_error).__name__)
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
            if operation == "update" and not isinstance(kwargs.get("event_id"), str): return "Error: event_id is required for update."
            path = config.WORKSPACE / ".threads" / thread_id / "calendar-invites" / f"microsoft-{uuid.uuid4().hex[:12]}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            data = json.dumps({"operation": operation, "event_id": kwargs.get("event_id"), "event": kwargs["event"]}, separators=(",", ":")).encode()
            path.write_bytes(data)
            relative = os.path.relpath(path, config.WORKSPACE)
            saved = await run_in_db(create_draft, thread_id, "invite", relative, data, {"account_id": account})
            return json.dumps({"draft_id": saved["draft_id"], "revision": saved["revision"], "path": relative, "next": "Show the person this calendar change and wait for their reply before creating or updating it."})
        except (MicrosoftError, ConnectedAccountError, DraftError) as exc: return _error(exc)


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
            if kwargs.get("account_id") != target.get("account_id"):
                return "Error: account_id does not match the draft."
            account = await _account(kwargs)
            result = await (calendar.create_event(account, payload["event"]) if self.operation == "create" else calendar.update_event(account, payload["event_id"], payload["event"]))
            await run_in_db(mark_sent, draft_id, str(result["id"]))
            return json.dumps(result)
        except (MicrosoftError, ConnectedAccountError, DraftError, ValueError, KeyError, TypeError) as exc: return _error(exc)


class MicrosoftCalendarCreateTool(_GatedCalendarTool):
    name = "microsoft_calendar_create"; description = "Create a reviewed Microsoft calendar draft."; operation = "create"
    parameters = {"type": "object", "additionalProperties": False, "required": ["account_id", "draft_id", "revision"], "properties": {"account_id": ACCOUNT_PARAM, "draft_id": {"type": "string"}, "revision": {"type": "string"}}}
class MicrosoftCalendarUpdateTool(_GatedCalendarTool):
    name = "microsoft_calendar_update"; description = "Update a reviewed Microsoft calendar draft."; operation = "update"
    parameters = {"type": "object", "additionalProperties": False, "required": ["account_id", "draft_id", "revision"], "properties": {"account_id": ACCOUNT_PARAM, "draft_id": {"type": "string"}, "revision": {"type": "string"}}}
