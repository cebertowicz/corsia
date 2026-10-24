"""Email through one of the person's Gmail or mail-host accounts: save a draft document, send it past the gate.

The draft is a workspace document the person reads and answers in chat. ``email_draft``
writes it, records its revision and binds it to the account it was written from;
``email_send`` sends it from that account, re-checking this thread's reach, only when
``tools.action_gate.check_action`` approves, and builds the message from the bytes the
gate checked, never from a second read. What differs by host is the account's backend
(``tools.mail_backends``).
"""

from __future__ import annotations

import json
import logging
from email.message import EmailMessage
from typing import Any

from connections.account_errors import AccountError
from connections.mail_accounts import MAIL_ACCOUNT_PARAM, resolve_for_thread
from connections.mail.email_document import (
    NEEDS_BODY,
    NEEDS_RECIPIENT,
    NEEDS_SUBJECT,
    DraftRefused,
    addresses,
    build_message,
    compose_document,
    parse_document,
    reply_subject,
    text_arg,
)
from infra.db_async import run_in_db
from tools.action_drafts import DraftError
from tools.reviewed_drafts import DraftStorageRefused, draft_target, open_draft, save_draft
from tools.action_gate import Approved, check_action
from tools.base import Tool
from tools.mail_backends import Gate, MailBackend, backend_for

log = logging.getLogger(__name__)

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
    "Not sent: the From line doesn't match the email account this draft was written from "
    "({account}). Save the draft again with email_draft and show the person."
)
OTHER_ACCOUNT = (
    "This draft was written from {address}. To use another account, save a new draft "
    "(omit draft_id) with that account_id and show the person."
)
NO_ACCOUNT = (
    "Not sent: this draft isn't tied to an email account. Save it again with email_draft and "
    "account_id, show the person, and wait for their reply."
)
GATE = Gate("email_send", STILL_SENDING, "Not sent: couldn't check the draft. Try again.")


# Reviewed-draft storage is shared by provider families in tools.reviewed_drafts.

class EmailDraftTool(Tool):
    self_describing_errors = True

    @property
    def name(self) -> str:
        return "email_draft"

    @property
    def description(self) -> str:
        return (
            "Save an email as a draft document the person can read, from the account named "
            "by account_id. Gmail accounts append their signature. Nothing is sent. Returns "
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
                       "description": "Plain recipient addresses; at least one, unless replying on a mail host."},
                "cc": {"type": "array", "items": {"type": "string"}, "description": "Plain cc addresses."},
                "subject": {"type": "string", "description": "One line."},
                "body": {"type": "string", "description": "The message text, without a signature."},
                "reply_to_message_id": {"type": "string", "description": "The message id (not the thread id), from email_search or email_read on this account, to reply under. On a mail host the recipient defaults to the message's Reply-To, else its sender, and the subject gets Re: when it lacks it."},
                "account_id": MAIL_ACCOUNT_PARAM,
            },
            "required": ["subject", "body"],
        }

    async def execute(self, **kwargs: Any) -> str:
        thread_id = kwargs.get("_thread_id")
        try:
            account = await run_in_db(resolve_for_thread, kwargs.get("account_id"), thread_id)
            backend = backend_for(account)
        except AccountError as exc:
            return f"Error: {exc}"
        try:
            return await self._save(str(thread_id), account, backend, kwargs)
        except DraftRefused as exc:
            return f"Error: {exc}"
        except AccountError as exc:
            return f"Error: The draft was not saved. {exc}"
        except DraftError as exc:
            return f"Error: {exc}"
        except Exception:
            log.exception("email_draft failed")
            return "Error: Couldn't save the draft."

    async def _save(self, thread_id: str, account: dict, backend: MailBackend, kwargs: dict[str, Any]) -> str:
        account_id, address = str(account["id"]), str(account["address"])
        to = addresses(kwargs.get("to"))
        if not to and not kwargs.get("reply_to_message_id"):
            raise DraftRefused(NEEDS_RECIPIENT)
        cc = addresses(kwargs.get("cc"))
        subject = text_arg(kwargs.get("subject"), NEEDS_SUBJECT, one_line=True)
        body = text_arg(kwargs.get("body"), NEEDS_BODY)
        draft_id = kwargs.get("draft_id")
        existing = await run_in_db(open_draft, thread_id, str(draft_id), "email") if draft_id is not None else None
        if existing is not None and existing["state"] != "open":
            raise DraftRefused(NO_OPEN_DRAFT)
        target, reply_label, signature = await backend.draft_metadata(account, existing, kwargs.get("reply_to_message_id"))
        to = to or target.get("reply_addresses") or []
        if not to:
            raise DraftRefused(NEEDS_RECIPIENT)
        if target.get("prefix_reply_subject"):
            subject = reply_subject(subject)
        target = {**target, "account_id": account_id, "account": address}
        text = compose_document(address, to, cc, subject, body, signature, reply_label)
        try:
            saved = await save_draft(
                thread_id=thread_id, kind="email", extension=".md", prefix="email-drafts",
                account_id=account_id, target=target, text=text, draft_id=draft_id,
                context=kwargs, description="Email draft; the person reviews it before anything is sent.",
            )
        except DraftStorageRefused as exc:
            raise DraftRefused(str(exc)) from None
        # The reviewed draft is already durable; a host-side copy only adds a note.
        copy_note = await backend.keep_draft_copy(account, text, target, draft_target(existing))
        relative = str((await run_in_db(open_draft, thread_id, saved["draft_id"], "email"))["path"])
        result = {
            "draft_id": saved["draft_id"], "revision": saved["revision"], "path": relative,
            "from": address, "account_id": account_id, "next": AFTER_DRAFT,
        }
        if copy_note is not None:
            result["note"] = copy_note
        return json.dumps(result)


# Reading and account binding are owned by tools.reviewed_drafts.

class EmailSendTool(Tool):
    self_describing_errors = True

    @property
    def name(self) -> str:
        return "email_send"

    @property
    def description(self) -> str:
        return (
            "Send a saved email draft from the email account it was written from. Call it only "
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
        backend, account, message, target = prepared
        return await backend.send_email(draft_id, account, message, target, GATE)

    async def _prepare(
        self, thread_id: Any, draft_id: str, revision: str,
    ) -> tuple[MailBackend, dict, EmailMessage, dict] | str:
        """The account's backend, the account, the message and the draft's target for an approved draft, or the error."""
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
            backend = backend_for(account)
        except AccountError as exc:
            return f"Error: Not sent: {exc}"
        try:
            parsed = parse_document(verdict.data.decode("utf-8"))
            if parsed is None:
                return f"Error: {BAD_DOCUMENT}"
            # Display only: the sender is always the bound account, never this line.
            if parsed["from"] is not None and parsed["from"].lower() != str(account["address"]).lower():
                return f"Error: {FROM_MISMATCH.format(account=account['address'])}"
            message = build_message(parsed, target)
        except (UnicodeDecodeError, ValueError):
            return f"Error: {BAD_DOCUMENT}"
        return backend, account, message, target
