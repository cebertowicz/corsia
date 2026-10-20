"""Email through one of the person's Google accounts: save a draft document, send it past the gate.

The draft is a workspace document the person reads and answers in chat. ``email_draft``
writes it, records its revision and binds it to the account it was written from;
``email_send`` sends it from that account, re-checking this thread's reach, only when
``tools.action_gate.check_action`` approves, and builds the message from the bytes the
gate checked, never from a second read. Every Gmail URL is a fixed template with ids
URL-encoded, so no model-built URL reaches the HTTP client.
"""

from __future__ import annotations

import json
import logging
from base64 import urlsafe_b64encode
from typing import Any

from connections.google import client as google_client
from connections.google.accounts import ACCOUNT_PARAM, resolve_for_thread, thread_key
from connections.google.client import GoogleError, GoogleRequestFailed
from connections.mail.email_document import (
    NEEDS_BODY,
    NEEDS_RECIPIENT,
    NEEDS_SUBJECT,
    DraftRefused,
    addresses,
    build_message,
    compose_document,
    parse_document,
    text_arg,
)
from connections.google.email_gmail import GMAIL, fetch_signature, kept_reply_label, reply_target
from infra import config
from infra.db_async import run_in_db
from tools.action_delivery import deliver_claimed
from tools.action_drafts import DraftError
from tools.reviewed_drafts import DraftStorageRefused, draft_target, open_draft, save_draft
from tools.action_gate import Approved, check_action
from tools.base import Tool

log = logging.getLogger(__name__)

SEND_URL = f"{GMAIL}/messages/send"
DRAFT_DIR = "email-drafts"

NO_OPEN_DRAFT = "There is no open draft with that id in this conversation."
BAD_DOCUMENT = (
    "Not sent: the draft document isn't in the draft format. Save it again with email_draft, "
    "show the person, and wait for their reply."
)
STILL_SENDING = (
    "Not sent: an earlier send of this draft didn't finish, so it may already have gone out. "
    "Check the person's Sent mail; if it isn't there, save a new draft with email_draft and "
    "show the person."
)
AFTER_DRAFT = (
    "Show the person the draft (link its path), end your turn, and call email_send only when "
    "their reply says to send it; if they ask for changes, save again with email_draft and "
    "show them the new version."
)
FROM_MISMATCH = (
    "Not sent: the From line doesn't match the Google account this draft was written from "
    "({account}). Save the draft again with email_draft and show the person."
)
OTHER_ACCOUNT = (
    "This draft was written from {address}. To use another account, save a new draft "
    "(omit draft_id) with that account_id and show the person."
)
NO_ACCOUNT = (
    "Not sent: this draft isn't tied to a Google account. Save it again with email_draft and "
    "account_id, show the person, and wait for their reply."
)
UNCONFIRMED = (
    "Google didn't confirm the send, so the email may have gone out. Don't send this draft "
    "again: check the person's Sent mail first."
)


# Reviewed-draft storage is shared by provider families in tools.reviewed_drafts.

class EmailDraftTool(Tool):
    self_describing_errors = True

    @property
    def name(self) -> str:
        return "email_draft"

    @property
    def description(self) -> str:
        return (
            "Save an email as a draft document the person can read, from the Gmail account named "
            "by account_id, with that account's signature appended. Nothing is sent. Returns "
            "draft_id and revision, which email_send needs. It is sent from the same account. "
            "Pass draft_id to replace an open draft; every field is required again, so "
            "send the whole message. Pass reply_to_message_id, a message id from the same account, "
            f"to thread the email under that message. {AFTER_DRAFT}"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object", "additionalProperties": False,
            "properties": {
                "draft_id": {"type": "string", "description": "An open draft to replace; omit for a new one."},
                "to": {"type": "array", "items": {"type": "string"},
                       "description": "Plain recipient addresses; at least one."},
                "cc": {"type": "array", "items": {"type": "string"}, "description": "Plain cc addresses."},
                "subject": {"type": "string", "description": "One line."},
                "body": {"type": "string", "description": "The message text, without a signature."},
                "reply_to_message_id": {"type": "string", "description": "The Gmail message id (not the thread id) to reply under."},
                "account_id": ACCOUNT_PARAM,
            },
            "required": ["to", "subject", "body"],
        }

    async def execute(self, **kwargs: Any) -> str:
        thread_id = kwargs.get("_thread_id")
        try:
            account = await run_in_db(resolve_for_thread, kwargs.get("account_id"), thread_id)
        except GoogleError as exc:
            return f"Error: {exc}"
        try:
            return await self._save(str(thread_id), account, kwargs)
        except DraftRefused as exc:
            return f"Error: {exc}"
        except GoogleError as exc:
            return f"Error: The draft was not saved. {exc}"
        except DraftError as exc:
            return f"Error: {exc}"
        except Exception:
            log.exception("email_draft failed")
            return "Error: Couldn't save the draft."

    async def _save(self, thread_id: str, account: dict, kwargs: dict[str, Any]) -> str:
        from tools.filesystem import WriteFileTool

        account_id, address = str(account["id"]), str(account["address"])
        to = addresses(kwargs.get("to"))
        if not to:
            raise DraftRefused(NEEDS_RECIPIENT)
        cc = addresses(kwargs.get("cc"))
        subject = text_arg(kwargs.get("subject"), NEEDS_SUBJECT, one_line=True)
        body = text_arg(kwargs.get("body"), NEEDS_BODY)
        draft_id = kwargs.get("draft_id")
        existing = await run_in_db(open_draft, thread_id, str(draft_id), "email") if draft_id is not None else None
        if existing is not None and existing["state"] != "open":
            raise DraftRefused(NO_OPEN_DRAFT)
        reply_id = kwargs.get("reply_to_message_id")
        if reply_id:
            target, reply_label = await reply_target(account_id, str(reply_id))
        else:
            target, reply_label = draft_target(existing), await kept_reply_label(account_id, existing)
        target = {**target, "account_id": account_id, "account": address}
        signature = await fetch_signature(account_id)
        text = compose_document(address, to, cc, subject, body, signature, reply_label)
        try:
            saved = await save_draft(
                thread_id=thread_id, kind="email", extension=".md", prefix="email-drafts",
                account_id=account_id, target=target, text=text, draft_id=draft_id,
                context=kwargs, description="Email draft; the person reviews it before anything is sent.",
            )
        except DraftStorageRefused as exc:
            raise DraftRefused(str(exc)) from None
        relative = str((await run_in_db(open_draft, thread_id, saved["draft_id"], "email"))["path"])
        return json.dumps({
            "draft_id": saved["draft_id"], "revision": saved["revision"], "path": relative,
            "from": address, "account_id": account_id,
            "next": AFTER_DRAFT,
        })


# Reading and account binding are owned by tools.reviewed_drafts.

class EmailSendTool(Tool):
    self_describing_errors = True

    @property
    def name(self) -> str:
        return "email_send"

    @property
    def description(self) -> str:
        return (
            "Send a saved email draft from the Google account it was written from. Call it only "
            "when the person's latest message says to send this draft. It goes out only if they "
            "wrote after the draft's last save and revision matches the one email_draft returned; "
            "otherwise nothing is sent and the error says what to do."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object", "additionalProperties": False,
            "properties": {
                "draft_id": {"type": "string", "description": "The draft_id email_draft returned."},
                "revision": {"type": "string", "description": "The revision email_draft returned."},
            },
            "required": ["draft_id", "revision"],
        }

    async def execute(self, **kwargs: Any) -> str:
        draft_id, revision = kwargs.get("draft_id"), kwargs.get("revision")
        if not isinstance(draft_id, str) or not draft_id or not isinstance(revision, str) or not revision:
            return "Error: Pass the draft_id and revision email_draft returned."
        try:
            prepared = await self._prepare(kwargs.get("_thread_id"), draft_id, revision)
        except Exception:
            log.exception("email_send could not check the draft")
            return "Error: Not sent: couldn't check the draft. Try again."
        if isinstance(prepared, str):
            return prepared
        return await self._deliver(draft_id, *prepared)

    async def _prepare(self, thread_id: Any, draft_id: str, revision: str) -> tuple[str, dict] | str:
        """The sending account and Gmail request body for an approved draft, or the error text."""
        row = await run_in_db(open_draft, str(thread_id), draft_id, "email")
        if row is not None and row["state"] == "sending":
            return f"Error: {STILL_SENDING}"
        verdict = await check_action("email", draft_id, revision)
        if not isinstance(verdict, Approved):
            return f"Error: {verdict.text}"
        target = draft_target(verdict.draft)
        if not target.get("account_id"):
            return f"Error: {NO_ACCOUNT}"
        try:
            account = await run_in_db(resolve_for_thread, target["account_id"], thread_id)
        except GoogleError as exc:
            return f"Error: Not sent: {exc}"
        try:
            parsed = parse_document(verdict.data.decode("utf-8"))
            if parsed is None:
                return f"Error: {BAD_DOCUMENT}"
            # Display only: the sender is always the bound account, never this line.
            if parsed["from"] is not None and parsed["from"].lower() != str(account["address"]).lower():
                return f"Error: {FROM_MISMATCH.format(account=account['address'])}"
            raw = urlsafe_b64encode(build_message(parsed, target).as_bytes()).decode("ascii")
        except (UnicodeDecodeError, ValueError):
            return f"Error: {BAD_DOCUMENT}"
        body: dict[str, Any] = {"raw": raw}
        if target.get("thread_id"):
            body["threadId"] = target["thread_id"]
        return str(account["id"]), body

    async def _deliver(self, draft_id: str, account_id: str, body: dict) -> str:
        """Send through the shared claimed-delivery lifecycle."""
        return await deliver_claimed(
            draft_id,
            acquire=lambda: google_client.access_token(account_id),
            send=lambda bearer: google_client.request_json(
                "POST", SEND_URL, account_id=account_id, body=body, token=bearer,
            ),
            result_id=lambda sent: sent.get("id") if isinstance(sent, dict) else None,
            success=lambda sent, message_id: json.dumps({
                "sent_message_id": message_id,
                "thread_id": thread_key(account_id, sent.get("threadId")),
                "account_id": account_id,
            }),
            sign_in_error=lambda exc: str(exc) if isinstance(exc, GoogleError) else "couldn't reach Google. Try again later.",
            request_error=str,
            never_sent=lambda exc: isinstance(exc, google_client.GoogleSignInRejected)
            or isinstance(exc, GoogleRequestFailed) and exc.never_sent,
            still_sending=STILL_SENDING,
            claim_error="Not sent: couldn't check the draft. Try again.",
            unconfirmed=UNCONFIRMED,
            label="email_send",
        )
