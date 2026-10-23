"""Calendar tools through one of the person's Google accounts, named on every call.

Invites follow the email draft/send lifecycle: the draft document is what the
person reviews, action_drafts owns its state and the account it is bound to,
and the action gate approves the exact revision before an event can be created.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any
from urllib.parse import quote

from connections import mail_accounts
from connections.mail_accounts import MAIL_ACCOUNT_PARAM
from connections.account_errors import AccountError
from connections.connected_accounts import MAILHOST_PROVIDER
from connections.google import client as google_client
from connections.google.accounts import ACCOUNT_PARAM
from connections.calendar_document import OFFSET_REQUIRED, compose_document, parse_time, read_draft, read_invite
from connections.google.calendar_document import event_summaries
from connections.google.client import GoogleError, GoogleRequestFailed
from connections.mailhost import agenda, outbound as mailhost_outbound
from connections.mailhost.errors import PASSWORD_REFUSED, MailHostError
from infra import config
from infra.db_async import run_in_db
from tools.action_delivery import deliver_claimed
from tools.action_drafts import DraftError
from tools.reviewed_drafts import DraftStorageRefused, draft_target, open_draft, save_draft
from tools.action_gate import Approved, Refusal, STALE_REVISION, check_action
from tools.base import Tool

log = logging.getLogger(__name__)

CALENDAR = "https://www.googleapis.com/calendar/v3"
DRAFT_DIR = "calendar-invites"
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
OTHER_ACCOUNT = "This invite was drafted from {address}; save a new one (omit draft_id) to use another account."


async def _account(kwargs: dict[str, Any]) -> dict:
    """The Gmail or mail-host account the call names and this thread may use, or :class:`AccountError`."""
    return await run_in_db(mail_accounts.resolve_for_thread, kwargs.get("account_id"), kwargs.get("_thread_id"))


# Reviewed-draft storage is shared by provider families in tools.reviewed_drafts.

def _event_url(calendar_id: str) -> str:
    return f"{CALENDAR}/calendars/{quote(calendar_id, safe='')}/events"


class CalendarListTool(Tool):
    self_describing_errors = True

    @property
    def name(self) -> str:
        return "calendar_list"

    @property
    def description(self) -> str:
        return (
            "List the Google Calendar events of the account named by account_id, in a range of up to 31 days\n"
            "An account on another email host lists its calendar the same way."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object", "additionalProperties": False,
            "properties": {
                "time_min": {"type": "string", "description": "RFC3339 time with a UTC offset."},
                "time_max": {"type": "string", "description": "RFC3339 time with a UTC offset."},
                "calendar_id": {"type": "string", "description": "Google calendar id; defaults to primary."},
                "account_id": MAIL_ACCOUNT_PARAM,
            },
            "required": ["time_min", "time_max"],
        }

    async def execute(self, **kwargs: Any) -> str:
        try:
            account = await _account(kwargs)
        except AccountError as exc:
            return f"Error: {exc}"
        account_id = str(account["id"])
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
            if account.get("provider") == MAILHOST_PROVIDER:
                if calendar_id != "primary":
                    return "Error: This account lists all its calendars together; leave calendar_id out."
                return json.dumps({"events": await agenda.list_events(account_id, start, end)})
            response = await google_client.get_json(account_id, _event_url(calendar_id), params={
                "timeMin": time_min, "timeMax": time_max, "maxResults": 250,
                "singleEvents": "true", "orderBy": "startTime",
            })
        except ValueError as exc:
            return f"Error: {exc}"
        except AccountError as exc:
            return f"Error: {exc}"
        except Exception:
            log.exception("calendar_list failed")
            return "Error: Couldn't read the calendar. Try again later."
        items = response.get("items") if isinstance(response, dict) else None
        result: dict[str, Any] = {"events": event_summaries(items)}
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
        return ("Save a calendar invite from the account named by account_id as a draft document for "
                "the person to review; nothing is sent, and it is later sent from the same account")

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
                "account_id": ACCOUNT_PARAM,
            },
            "required": ["title", "start", "end", "attendees"],
        }

    async def execute(self, **kwargs: Any) -> str:
        try:
            account = await _account(kwargs)
        except AccountError as exc:
            return f"Error: {exc}"
        try:
            return await self._save(str(kwargs["_thread_id"]), account, kwargs)
        except ValueError as exc:
            return f"Error: {exc}"
        except AccountError as exc:
            return f"Error: The invite was not saved. {exc}"
        except DraftError as exc:
            return f"Error: {exc}"
        except Exception:
            log.exception("calendar_draft_invite failed")
            return "Error: Couldn't save the invite draft."

    async def _save(self, thread_id: str, account: dict, kwargs: dict[str, Any]) -> str:
        invite = read_invite(kwargs)
        draft_id = kwargs.get("draft_id")
        email = str(account["address"])
        text = compose_document(email, invite.title, invite.start, invite.end, invite.attendees, invite.description)
        target = {"account": email, "account_id": account["id"], "title": invite.title, "start": invite.start,
                  "end": invite.end, "attendees": invite.attendees, "description": invite.description}
        try:
            saved = await save_draft(
                thread_id=thread_id, kind="invite", extension=".md", prefix="calendar-invites",
                account_id=str(account["id"]), target=target, text=text, draft_id=draft_id,
                context=kwargs, description="Calendar invite draft; the person reviews it before anything is sent.",
            )
        except DraftStorageRefused as exc:
            raise ValueError(str(exc)) from None
        path = (await run_in_db(open_draft, thread_id, saved["draft_id"], "invite"))["path"]
        return json.dumps({"draft_id": saved["draft_id"], "revision": saved["revision"], "path": path,
                           "from": email, "account_id": account["id"], "next": AFTER_DRAFT})

def _google_invite_body(invite) -> dict[str, Any]:
    return {"summary": invite.title, "start": {"dateTime": invite.start}, "end": {"dateTime": invite.end},
            "attendees": [{"email": email} for email in invite.attendees], "description": invite.description}


def _refused_before_send(exc: Exception) -> bool:
    return isinstance(exc, MailHostError) and exc.cause == PASSWORD_REFUSED


def _mailhost_invite_body(invite):
    return invite


_INVITE_BODIES = {"google": _google_invite_body, "mailhost": _mailhost_invite_body}


class CalendarSendInviteTool(Tool):
    self_describing_errors = True

    @property
    def name(self) -> str:
        return "calendar_send_invite"

    @property
    def description(self) -> str:
        return ("Send a saved invite draft, from the account it was drafted from, after the person "
                "says to send it; attendees get emails")

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
        draft_id, revision = kwargs.get("draft_id"), kwargs.get("revision")
        if not isinstance(draft_id, str) or not draft_id or not isinstance(revision, str) or not revision:
            return "Error: Pass the draft_id and revision calendar_draft_invite returned."
        try:
            prepared = await self._prepare(kwargs.get("_thread_id"), draft_id, revision)
        except Exception:
            log.exception("calendar_send_invite could not check the invite")
            return "Error: Not sent: couldn't check the invite draft. Try again."
        if isinstance(prepared, str):
            return prepared
        return await self._deliver(draft_id, *prepared)

    async def _prepare(self, thread_id: Any, draft_id: str, revision: str) -> tuple[dict, Any] | str:
        row = await run_in_db(open_draft, str(thread_id), draft_id, "invite")
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
            draft = read_draft(verdict.data, verdict.draft.get("target_json"), require_account_id=True)
        except (UnicodeDecodeError, ValueError):
            return f"Error: {BAD_DOCUMENT}"
        try:
            account = await run_in_db(mail_accounts.resolve_for_thread, draft.account_id, thread_id)
        except AccountError as exc:
            return f"Error: Not sent: {exc}"
        if str(account["address"]).lower() != draft.address.lower():
            return f"Error: {BAD_DOCUMENT}"
        body_builder = _INVITE_BODIES.get(str(account["provider"]))
        if body_builder is None:
            return "Error: Not sent: this account can't send invitations."
        return account, body_builder(draft.invite)

    async def _deliver(self, draft_id: str, account: dict, body: Any) -> str:
        deliver = {"google": self._deliver_google, "mailhost": self._deliver_mailhost}.get(str(account["provider"]))
        if deliver is None:
            return "Error: Not sent: this account can't send invitations."
        return await deliver(draft_id, account, body)

    async def _deliver_google(self, draft_id: str, account: dict, body: dict) -> str:
        account_id = str(account["id"])
        return await deliver_claimed(
            draft_id, acquire=lambda: google_client.access_token(account_id), provider_errors=(GoogleError,),
            send=lambda bearer: google_client.request_json("POST", _event_url("primary"), account_id=account_id,
                body=body, params={"sendUpdates": "all"}, token=bearer),
            result_id=lambda sent: sent.get("id") if isinstance(sent, dict) else None,
            success=lambda sent, event_id: json.dumps({"event_id": event_id,
                "html_link": sent.get("htmlLink") if isinstance(sent, dict) else None}),
            sign_in_error=lambda exc: str(exc) if isinstance(exc, GoogleError) else "couldn't reach Google. Try again later.",
            request_error=str, never_sent=lambda exc: isinstance(exc, google_client.GoogleSignInRejected)
            or isinstance(exc, GoogleRequestFailed) and exc.never_sent, still_sending=STILL_SENDING,
            claim_error="Not sent: couldn't check the invite draft. Try again.", unconfirmed=UNCONFIRMED,
            label="calendar_send_invite",
        )

    async def _deliver_mailhost(self, draft_id: str, account: dict, body: Any) -> str:
        account_id, address = str(account["id"]), str(account["address"])
        async def no_token() -> None:
            return None
        return await deliver_claimed(
            draft_id, acquire=no_token, provider_errors=(GoogleError,),
            send=lambda _token: mailhost_outbound.send_invite(account_id, address, body), result_id=lambda result: result,
            success=lambda _result, event_id: json.dumps({"event_id": event_id}), sign_in_error=str,
            request_error=str, never_sent=_refused_before_send, still_sending=STILL_SENDING,
            claim_error="Not sent: couldn't check the invite draft. Try again.",
            unconfirmed="The mail host didn't confirm the invite send, so it may have gone out. Check the person's calendar first.",
            label="calendar_send_invite",
        )
